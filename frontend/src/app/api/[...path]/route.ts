// frontend/src/app/api/[...path]/route.ts
import { NextRequest } from "next/server";
import crypto from "crypto";
import { shouldStreamRequestBody } from "@/lib/proxyStreaming";

const ALLOWED_HEADERS = [
  "authorization",
  "content-type",
  "accept",
  "user-agent",
  "x-request-id",
  // Auth par cookie HttpOnly : on transmet le cookie au backend Django…
  "cookie",
  // …et le header CSRF (double-submit) pour les requêtes mutantes.
  "x-csrftoken",
];

// Headers that must NEVER be forwarded to upstream (hop-by-hop / computed by fetch)
const STRIP_REQ_HEADERS = new Set([
  "content-length",
  "host",
  "connection",
  "transfer-encoding",
  "keep-alive",
  "proxy-connection",
  "upgrade",
  "te",
  "trailer",
]);

function getBackendBase(): string {
  const v = process.env.BACKEND_BASE_URL;
  if (!v) {
    // Fail-fast at runtime only (no breaking next build)
    throw new Error("BACKEND_BASE_URL is required");
  }
  return v.replace(/\/+$/, "");
}

function pickHeaders(req: NextRequest) {
  const out = new Headers();
  for (const key of ALLOWED_HEADERS) {
    const v = req.headers.get(key);
    if (v) out.set(key, v);
  }

  // extra safety: never forward forbidden headers even if they sneak in later
  for (const k of STRIP_REQ_HEADERS) out.delete(k);

  return out;
}

function ensureRequestId(req: NextRequest, headers: Headers): string {
  const existing = req.headers.get("x-request-id");
  const rid = existing && existing.trim() ? existing : crypto.randomUUID();
  headers.set("x-request-id", rid);
  return rid;
}

function isSafeLocation(location: string, backendBase: string) {
  if (location.startsWith("/")) return true;
  try {
    const u = new URL(location);
    const backendOrigin = new URL(backendBase).origin;
    return u.origin === backendOrigin;
  } catch {
    return false;
  }
}

// Next 15 : `params` d'un route handler est une Promise (API dynamique asynchrone).
type RouteContext = { params: Promise<{ path: string[] }> };

async function handler(req: NextRequest, ctx: RouteContext) {
  const backendBase = getBackendBase();

  const { path: pathSegments } = await ctx.params;
  const path = pathSegments.join("/");
  const url = new URL(req.url);

  const hasTrailingSlash = url.pathname.endsWith("/");
  const target = `${backendBase}/api/${path}${hasTrailingSlash ? "/" : ""}${url.search}`;

  const headers = pickHeaders(req);
  const requestId = ensureRequestId(req, headers);

  const method = req.method.toUpperCase();

  const init: RequestInit = {
    method,
    headers,
    redirect: "manual",
    cache: "no-store",
  };

  // Only attach body when we actually have one.
  // This avoids UND_ERR_REQ_CONTENT_LENGTH_MISMATCH for DELETE/POST with empty body.
  const canHaveBody = method !== "GET" && method !== "HEAD";
  const contentLength = (req.headers.get("content-length") || "").trim();
  const hasIncomingBody = canHaveBody && contentLength !== "" && contentLength !== "0";

  // Dépôt audio : corps relayé EN FLUX (jusqu'à 600 Mo), jamais
  // bufferisé en mémoire. Toutes les autres routes gardent le comportement historique.
  const streamBody = hasIncomingBody && req.body != null && shouldStreamRequestBody(pathSegments);

  if (streamBody) {
    init.body = req.body as any;
    // @ts-ignore (Node/undici specific)
    (init as any).duplex = "half";
  } else if (hasIncomingBody) {
    const ab = await req.arrayBuffer();
    if (ab.byteLength > 0) {
      init.body = ab as any;
      // @ts-ignore (Node/undici specific)
      (init as any).duplex = "half";
    }
  }

  // Never set/forward Host: let fetch compute it for the target URL.
  headers.delete("host");
  // Never forward content-length: undici will set it correctly (or omit it).
  headers.delete("content-length");
  // …sauf pour un corps relayé en flux : sans longueur, Django ne peut pas analyser le
  // multipart (CONTENT_LENGTH). On transmet alors la longueur ENTRANTE, inchangée.
  if (streamBody) headers.set("content-length", contentLength);

  const res = await fetch(target, init);

  const outHeaders = new Headers(res.headers);

  // Set-Cookie: préserver CHAQUE cookie séparément (le login pose 2 cookies:
  // auth HttpOnly + csrftoken). `new Headers()` peut les replier en une seule
  // valeur jointe par virgule, ce qui casserait les attributs — on réécrit donc
  // explicitement via getSetCookie().
  const setCookies =
    typeof (res.headers as any).getSetCookie === "function"
      ? (res.headers as any).getSetCookie()
      : [];
  if (setCookies.length > 0) {
    outHeaders.delete("set-cookie");
    for (const c of setCookies) outHeaders.append("set-cookie", c);
  }

  // Strip hop-by-hop / problematic response headers
  outHeaders.delete("content-encoding");
  outHeaders.delete("connection");

  // Prevent unsafe redirects
  const loc = outHeaders.get("location");
  if (loc && !isSafeLocation(loc, backendBase)) {
    outHeaders.delete("location");
  }

  // Always expose request id to the browser for correlation
  outHeaders.set("x-request-id", requestId);

  // SSE hardening: disable buffering/caching for event streams
  const accept = req.headers.get("accept") || "";
  const isSse =
    accept.includes("text/event-stream") ||
    (outHeaders.get("content-type") || "").includes("text/event-stream");
  if (isSse) {
    outHeaders.set("cache-control", "no-cache");
    outHeaders.set("x-accel-buffering", "no");
  }

  return new Response(res.body, { status: res.status, headers: outHeaders });
}

export {
  handler as GET,
  handler as POST,
  handler as PUT,
  handler as PATCH,
  handler as DELETE,
  handler as OPTIONS,
};
