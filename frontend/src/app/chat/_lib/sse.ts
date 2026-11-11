// frontend/src/app/chat/_lib/sse.ts
import { getCsrfToken } from "@/lib/auth";

export type SseEventHandler = (eventName: string, data: any) => void;

export type ReadSseStreamArgs = {
  url: string;
  body: any;
  signal?: AbortSignal;
  onEvent: SseEventHandler;
};

function safeJsonParse(s: string) {
  try {
    return JSON.parse(s);
  } catch {
    return null;
  }
}

async function safeReadText(res: Response): Promise<string> {
  try {
    return await res.text();
  } catch {
    return "";
  }
}

function normalizeNewlines(s: string) {
  return s.replace(/\r\n/g, "\n");
}

export async function readSseStream({ url, body, signal, onEvent }: ReadSseStreamArgs): Promise<void> {
  const headers = new Headers();
  headers.set("Content-Type", "application/json");
  headers.set("Accept", "text/event-stream");
  // POST mutant + auth par cookie => header CSRF requis.
  const csrf = getCsrfToken();
  if (csrf) headers.set("X-CSRFToken", csrf);

  const res = await fetch(url, {
    method: "POST",
    headers,
    body: JSON.stringify(body ?? {}),
    signal,
    cache: "no-store",
    credentials: "include",
  });

  if (!res.ok) {
    const t = await safeReadText(res);
    const parsed = safeJsonParse(t);
    const msg =
      (parsed && (parsed.detail || parsed.error || parsed.message)) ||
      t ||
      `API ${res.status} ${res.statusText}`;
    throw new Error(msg);
  }

  if (!res.body) throw new Error("SSE: response body is empty");

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buf = "";

  // ✅ Keep a tiny state so downstream always receives log_id even if it only
  // uses the "done" event (some UIs ignore "meta").
  let lastLogId: string | null = null;

  while (true) {
    if (signal?.aborted) break;

    const { value, done } = await reader.read();
    if (done) break;

    buf += decoder.decode(value, { stream: true });
    buf = normalizeNewlines(buf);

    let idx: number;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);

      let eventName = "message";
      const dataLines: string[] = [];

      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) eventName = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }

      const dataStr = dataLines.join("\n").trim();
      if (!dataStr) continue;

      const parsed = safeJsonParse(dataStr);
      const payload: any = parsed ?? dataStr;

      // ✅ Capture log_id from meta/done and propagate it if missing on "done"
      if (payload && typeof payload === "object") {
        const maybeLogId = payload.log_id ?? payload.logId ?? null;
        if (typeof maybeLogId === "string" && maybeLogId) lastLogId = maybeLogId;

        if (eventName === "done" && !payload.log_id && lastLogId) {
          payload.log_id = lastLogId;
        }
      }

      onEvent(eventName, payload);
    }
  }
}
