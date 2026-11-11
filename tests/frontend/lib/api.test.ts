import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  apiGet,
  apiPost,
  apiPostForm,
  listRagSpaces,
  listDocuments,
  deleteDocument,
  summarizeDocument,
  getDocumentSummary,
  summarizeDocumentAwait,
  summaryErrorKey,
} from "@/lib/api";
import * as auth from "@/lib/auth";

function lastInitOf(fetchMock: ReturnType<typeof vi.fn>): RequestInit {
  return fetchMock.mock.calls.at(-1)![1] as RequestInit;
}

function jsonResponse(body: any, init: Partial<Response> = {}) {
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    statusText: init.statusText ?? "OK",
    headers: { get: (k: string) => (k.toLowerCase() === "content-type" ? "application/json" : null) },
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

describe("lib/api", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  function lastUrl() {
    return fetchMock.mock.calls.at(-1)![0] as string;
  }
  function lastInit() {
    return fetchMock.mock.calls.at(-1)![1] as RequestInit;
  }
  function headerVal(init: RequestInit, name: string): string | null {
    // api.ts construit un objet Headers
    return (init.headers as Headers).get(name);
  }

  describe("trailing slash", () => {
    it("ajoute un slash final aux chemins /api/...", async () => {
      await apiGet("/api/v1/x");
      expect(lastUrl()).toBe("/api/v1/x/");
    });
    it("préserve la query string", async () => {
      await apiGet("/api/v1/x?a=1");
      expect(lastUrl()).toBe("/api/v1/x/?a=1");
    });
    it("ne touche pas les chemins hors /api/", async () => {
      await apiGet("/other");
      expect(lastUrl()).toBe("/other");
    });
  });

  describe("auth par cookie (plus de header Authorization)", () => {
    it("n'émet jamais de header Authorization", async () => {
      await apiGet("/api/v1/x");
      expect(headerVal(lastInit(), "authorization")).toBeNull();
      await apiPost("/api/v1/x", { a: 1 });
      expect(headerVal(lastInit(), "authorization")).toBeNull();
    });

    it("envoie les cookies via credentials: include", async () => {
      await apiGet("/api/v1/x");
      expect(lastInit().credentials).toBe("include");
    });
  });

  describe("CSRF", () => {
    it("ajoute X-CSRFToken sur une méthode mutante quand le cookie existe", async () => {
      vi.spyOn(auth, "getCsrfToken").mockReturnValue("csrf-1");
      await apiPost("/api/v1/x", { a: 1 });
      expect(headerVal(lastInit(), "x-csrftoken")).toBe("csrf-1");
    });

    it("n'ajoute pas X-CSRFToken sur un GET (méthode sûre)", async () => {
      vi.spyOn(auth, "getCsrfToken").mockReturnValue("csrf-1");
      await apiGet("/api/v1/x");
      expect(headerVal(lastInit(), "x-csrftoken")).toBeNull();
    });

    it("n'ajoute pas X-CSRFToken sans cookie CSRF", async () => {
      vi.spyOn(auth, "getCsrfToken").mockReturnValue(null);
      await apiPost("/api/v1/x", { a: 1 });
      expect(headerVal(lastInit(), "x-csrftoken")).toBeNull();
    });
  });

  describe("Content-Type", () => {
    it("pose application/json pour un body non-FormData", async () => {
      await apiPost("/api/v1/x", { a: 1 });
      expect(headerVal(lastInit(), "content-type")).toContain("application/json");
      expect(lastInit().body).toBe(JSON.stringify({ a: 1 }));
    });
    it("apiPost sans data n'envoie pas de body", async () => {
      await apiPost("/api/v1/x");
      expect(lastInit().body).toBeUndefined();
    });
    it("ne force pas Content-Type pour FormData", async () => {
      const fd = new FormData();
      fd.append("k", "v");
      await apiPostForm("/api/v1/x", fd);
      expect(headerVal(lastInit(), "content-type")).toBeNull();
    });
  });

  describe("gestion des erreurs", () => {
    it("extrait le message depuis detail", async () => {
      fetchMock.mockResolvedValueOnce(
        jsonResponse({ detail: "boom" }, { ok: false, status: 400, statusText: "Bad Request" })
      );
      await expect(apiGet("/api/v1/x")).rejects.toThrow(/boom/);
    });
    it("extrait depuis non_field_errors", async () => {
      fetchMock.mockResolvedValueOnce(
        jsonResponse({ non_field_errors: ["invalide"] }, { ok: false, status: 400, statusText: "Bad Request" })
      );
      await expect(apiGet("/api/v1/x")).rejects.toThrow(/invalide/);
    });
  });

  describe("summarizeDocument", () => {
    it("POST sur l'endpoint summary du document avec scope/page", async () => {
      fetchMock.mockResolvedValue(jsonResponse({ ok: true, summary: "ok" }));
      await summarizeDocument("doc-42", { scope: "page", page: 7 });
      expect(lastUrl()).toBe("/api/v1/documents/doc-42/summary/");
      const body = JSON.parse(lastInit().body as string);
      expect(body).toMatchObject({ mode: "bullets", scope: "page", page: 7, language: "auto" });
    });

    it("défaut: scope document, mode bullets, langue auto (jamais fr forcé)", async () => {
      fetchMock.mockResolvedValue(jsonResponse({ ok: true, summary: "ok" }));
      await summarizeDocument("doc-1");
      const body = JSON.parse(lastInit().body as string);
      expect(body.scope).toBe("document");
      expect(body.page).toBeUndefined();
      // Régression gardée : un défaut `fr` ici forçait le français pour tous les
      // appelants et rendait tout résumé anglais impossible.
      expect(body.language).toBe("auto");
    });

    it("respecte une langue explicite", async () => {
      fetchMock.mockResolvedValue(jsonResponse({ ok: true, summary: "ok" }));
      await summarizeDocument("doc-1", { language: "en" });
      expect(JSON.parse(lastInit().body as string).language).toBe("en");
    });

    it("encode l'identifiant de document dans l'URL", async () => {
      fetchMock.mockResolvedValue(jsonResponse({ ok: true }));
      await summarizeDocument("a/b#c");
      expect(lastUrl()).toBe("/api/v1/documents/a%2Fb%23c/summary/");
    });
  });

  describe("getDocumentSummary", () => {
    it("GET avec mode/scope/language, + page si scope=page", async () => {
      fetchMock.mockResolvedValue(jsonResponse({ ok: true, status: "none" }));
      await getDocumentSummary("doc-9", { scope: "page", page: 4 });
      const url = lastUrl();
      expect(url).toContain("/api/v1/documents/doc-9/summary/");
      expect(url).toContain("scope=page");
      expect(url).toContain("page=4");
      expect(url).toContain("language=auto");
    });

    it("n'ajoute pas page hors scope=page", async () => {
      fetchMock.mockResolvedValue(jsonResponse({ ok: true, status: "ready" }));
      await getDocumentSummary("doc-9", {});
      expect(lastUrl()).not.toContain("page=");
    });
  });

  describe("summarizeDocumentAwait", () => {
    it("retourne directement si non-pending (pas de polling)", async () => {
      fetchMock.mockResolvedValue(jsonResponse({ ok: true, status: "ready", summary: "ok" }));
      const r = await summarizeDocumentAwait("doc-1", { mode: "bullets" });
      expect(r.status).toBe("ready");
      expect(fetchMock).toHaveBeenCalledTimes(1); // POST seul, aucun GET de polling
    });

    it("poll jusqu'à ready quand le POST renvoie pending", async () => {
      fetchMock
        .mockResolvedValueOnce(jsonResponse({ ok: true, status: "pending", summary: "" })) // POST
        .mockResolvedValueOnce(jsonResponse({ ok: true, status: "pending", summary: "" })) // GET 1
        .mockResolvedValueOnce(jsonResponse({ ok: true, status: "ready", summary: "fini" })); // GET 2
      const onPending = vi.fn();
      const r = await summarizeDocumentAwait("doc-2", {}, { onPending, intervalMs: 0 });
      expect(r.status).toBe("ready");
      expect(r.summary).toBe("fini");
      expect(onPending).toHaveBeenCalledTimes(1);
      expect(fetchMock).toHaveBeenCalledTimes(3);
    });

    it("poll la langue RÉSOLUE par le POST, pas la préférence d'origine", async () => {
      // Le POST est envoyé en `auto` ; le serveur résout `en` et crée la variante
      // `bullets:document::en`. Le polling doit viser CETTE variante, sinon il
      // interroge `…:auto` (inexistante) et ne termine jamais.
      fetchMock
        .mockResolvedValueOnce(jsonResponse({ ok: true, status: "pending", language: "en" })) // POST
        .mockResolvedValueOnce(jsonResponse({ ok: true, status: "ready", summary: "done", language: "en" }));
      const r = await summarizeDocumentAwait("doc-3", {}, { intervalMs: 0 });
      expect(r.status).toBe("ready");
      const postBody = JSON.parse((fetchMock.mock.calls[0]![1] as RequestInit).body as string);
      expect(postBody.language).toBe("auto"); // POST
      expect(lastUrl()).toContain("language=en"); // GET de polling
    });
  });

  describe("summaryErrorKey", () => {
    it("mappe les codes backend vers des CLÉS i18n, jamais du texte", () => {
      expect(summaryErrorKey("document_not_ready")).toBe("SUMMARY_NOT_READY");
      expect(summaryErrorKey("document_no_content")).toBe("SUMMARY_NO_CONTENT");
      expect(summaryErrorKey("summary_provider_unavailable")).toBe("SUMMARY_PROVIDER_UNAVAILABLE");
      expect(summaryErrorKey("summary_timeout")).toBe("SUMMARY_TIMEOUT");
      expect(summaryErrorKey("not_found")).toBe("SUMMARY_NOT_FOUND");
      expect(summaryErrorKey("")).toBe("SUMMARY_FAILED");
    });

    it("ne renvoie jamais de prose française en dur", () => {
      // Régression : la fonction rendait du français quelle que soit l'interface,
      // ce qui affichait un message français sous un résumé anglais.
      for (const code of ["document_not_ready", "not_found", "", "boom"]) {
        expect(summaryErrorKey(code)).toMatch(/^[A-Z_]+$/);
      }
    });
  });

  describe("listDocuments", () => {
    it("GET avec rag_space_id et limit", async () => {
      fetchMock.mockResolvedValue(jsonResponse({ items: [], total: 0 }));
      await listDocuments("rs-1", { limit: 50 });
      expect(lastUrl()).toContain("/api/v1/documents/");
      expect(lastUrl()).toContain("rag_space_id=rs-1");
      expect(lastUrl()).toContain("limit=50");
    });
  });

  describe("deleteDocument", () => {
    it("DELETE sur l'URL /delete/ du document", async () => {
      fetchMock.mockResolvedValue(jsonResponse({ ok: true, deleted: true, chunks_deleted: 3 }));
      const r = await deleteDocument("doc-9");
      expect(lastUrl()).toContain("/api/v1/documents/doc-9/delete/");
      expect(lastInit().method).toBe("DELETE");
      expect(r.deleted).toBe(true);
      expect(r.chunks_deleted).toBe(3);
    });
  });

  describe("listRagSpaces", () => {
    it("ajoute workspace_id quand fourni", async () => {
      fetchMock.mockResolvedValue(jsonResponse([]));
      await listRagSpaces({ workspaceId: 5 });
      expect(lastUrl()).toContain("workspace_id=5");
    });
    it("n'ajoute pas de query sans workspaceId", async () => {
      fetchMock.mockResolvedValue(jsonResponse([]));
      await listRagSpaces({});
      expect(lastUrl()).not.toContain("workspace_id");
    });
  });
});
