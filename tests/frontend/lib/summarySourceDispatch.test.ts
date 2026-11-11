/**
 * Dispatch du résumé par PROVENANCE de source.
 *
 * Le défaut d'origine : le frontend envoyait tout `document_id` vers
 * `/api/v1/documents/<id>/summary/`, y compris l'UUID d'un document de corpus
 * métier — que le backend cherche dans la table privée. Ces tests verrouillent
 * l'endpoint choisi pour chacune des trois provenances.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  getSourceDocumentSummary,
  summarizeSourceDocument,
  summarizeSourceDocumentAwait,
  summaryEndpointFor,
  summaryErrorKey,
} from "@/lib/api";
import * as auth from "@/lib/auth";

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

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true, status: "ready", summary: "S" }));
  vi.stubGlobal("fetch", fetchMock);
  vi.spyOn(auth, "getCsrfToken").mockReturnValue("csrf-token");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const lastUrl = () => String(fetchMock.mock.calls.at(-1)![0]);
const lastBody = () =>
  JSON.parse((fetchMock.mock.calls.at(-1)![1] as RequestInit).body as string);

describe("summaryEndpointFor", () => {
  it("source privée -> endpoint documents", () => {
    expect(
      summaryEndpointFor({ sourceType: "workspace_document", documentId: "d-1" })
    ).toBe("/api/v1/documents/d-1/summary/");
  });

  it("source de corpus métier -> endpoint dédié", () => {
    expect(summaryEndpointFor({ sourceType: "domain_corpus", documentId: "c-1" })).toBe(
      "/api/v1/domain-corpus-documents/c-1/summary/"
    );
  });

  it("provenance inconnue -> endpoint privé HISTORIQUE", () => {
    // Les sources antérieures à la v2 du contrat n'ont pas de `source_type` et
    // ne peuvent être que privées. Les router vers l'API corpus casserait tout
    // l'historique ; leur retirer l'action le punirait sans rien sécuriser.
    expect(summaryEndpointFor({ sourceType: "unknown", documentId: "old-1" })).toBe(
      "/api/v1/documents/old-1/summary/"
    );
  });

  it("l'identifiant est encodé", () => {
    expect(
      summaryEndpointFor({ sourceType: "domain_corpus", documentId: "a/b?c" })
    ).toBe("/api/v1/domain-corpus-documents/a%2Fb%3Fc/summary/");
  });
});

describe("summarizeSourceDocument", () => {
  it("privé : POST vers l'API documents, sans rag_space_id", async () => {
    await summarizeSourceDocument(
      { sourceType: "workspace_document", documentId: "d-1", ragSpaceId: "rs-1" },
      { scope: "document" }
    );

    expect(lastUrl()).toBe("/api/v1/documents/d-1/summary/");
    // `rag_space_id` n'a aucun sens pour un document privé : il porte déjà son
    // espace. L'envoyer laisserait croire qu'il est arbitrable.
    expect(lastBody()).not.toHaveProperty("rag_space_id");
  });

  it("corpus : POST vers l'API métier, avec l'espace CONSOMMATEUR", async () => {
    await summarizeSourceDocument(
      { sourceType: "domain_corpus", documentId: "c-1", ragSpaceId: "rs-9" },
      { scope: "page", page: 13 }
    );

    expect(lastUrl()).toBe("/api/v1/domain-corpus-documents/c-1/summary/");
    const body = lastBody();
    expect(body.rag_space_id).toBe("rs-9");
    expect(body.scope).toBe("page");
    expect(body.page).toBe(13);
  });

  it("payload document : scope document, pas de page", async () => {
    await summarizeSourceDocument({ sourceType: "domain_corpus", documentId: "c-1" });

    const body = lastBody();
    expect(body.scope).toBe("document");
    expect(body.page).toBeUndefined();
    expect(body.mode).toBe("bullets");
    expect(body.language).toBe("auto");
  });

  it("thread_id et request_id voyagent ensemble", async () => {
    await summarizeSourceDocument(
      { sourceType: "domain_corpus", documentId: "c-1", ragSpaceId: "rs-1" },
      { threadId: "t-1", requestId: "rq-1" }
    );

    const body = lastBody();
    expect(body.thread_id).toBe("t-1");
    expect(body.request_id).toBe("rq-1");
  });

  it("sans thread_id, aucun des deux n'est envoyé", async () => {
    await summarizeSourceDocument({ sourceType: "domain_corpus", documentId: "c-1" }, {});

    const body = lastBody();
    expect(body).not.toHaveProperty("thread_id");
    expect(body).not.toHaveProperty("request_id");
  });
});

describe("getSourceDocumentSummary (polling)", () => {
  it("corpus : GET sur l'endpoint métier avec rag_space_id", async () => {
    await getSourceDocumentSummary(
      { sourceType: "domain_corpus", documentId: "c-1", ragSpaceId: "rs-2" },
      { scope: "page", page: 4 }
    );

    const url = lastUrl();
    expect(url).toContain("/api/v1/domain-corpus-documents/c-1/summary/");
    expect(url).toContain("rag_space_id=rs-2");
    expect(url).toContain("page=4");
  });

  it("privé : GET sur l'endpoint documents, sans rag_space_id", async () => {
    await getSourceDocumentSummary({
      sourceType: "workspace_document",
      documentId: "d-1",
      ragSpaceId: "rs-2",
    });

    const url = lastUrl();
    expect(url).toContain("/api/v1/documents/d-1/summary/");
    expect(url).not.toContain("rag_space_id");
  });
});

describe("summarizeSourceDocumentAwait", () => {
  it("poll le MÊME endpoint que le POST", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ ok: true, status: "pending", language: "fr" }))
      .mockResolvedValueOnce(jsonResponse({ ok: true, status: "ready", summary: "fini" }));

    const onPending = vi.fn();
    const r = await summarizeSourceDocumentAwait(
      { sourceType: "domain_corpus", documentId: "c-1", ragSpaceId: "rs-3" },
      {},
      { intervalMs: 0, onPending }
    );

    expect(r.status).toBe("ready");
    expect(onPending).toHaveBeenCalledTimes(1);
    for (const call of fetchMock.mock.calls) {
      expect(String(call[0])).toContain("/api/v1/domain-corpus-documents/c-1/summary/");
    }
  });

  it("poll la langue RÉSOLUE par le POST", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ ok: true, status: "pending", language: "en" }))
      .mockResolvedValueOnce(jsonResponse({ ok: true, status: "ready", summary: "done" }));

    await summarizeSourceDocumentAwait(
      { sourceType: "domain_corpus", documentId: "c-1" },
      {},
      { intervalMs: 0 }
    );

    expect(lastUrl()).toContain("language=en");
  });

  it("remonte l'erreur 404 telle quelle (message traduisible)", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ detail: "not_found" }, { ok: false, status: 404, statusText: "Not Found" })
    );

    await expect(
      summarizeSourceDocumentAwait({ sourceType: "domain_corpus", documentId: "c-1" })
    ).rejects.toThrow(/not_found/);
  });

  it("remonte le 409 de conflit de request_id", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        { detail: "request_id_conflict" },
        { ok: false, status: 409, statusText: "Conflict" }
      )
    );

    await expect(
      summarizeSourceDocumentAwait({ sourceType: "domain_corpus", documentId: "c-1" })
    ).rejects.toThrow(/request_id_conflict/);
  });
});

describe("summaryErrorKey — codes du chemin corpus", () => {
  it("conflit de request_id a sa propre clé", () => {
    // Avant `not_found` dans la chaîne de tests : le corps du 409 contient les
    // deux motifs et la recherche de sous-chaîne prendrait le premier.
    expect(summaryErrorKey("API 409 Conflict: request_id_conflict")).toBe(
      "SUMMARY_REQUEST_CONFLICT"
    );
  });

  it("404 du corpus se traduit comme le 404 privé (aucun oracle)", () => {
    expect(summaryErrorKey("API 404 Not Found: not_found")).toBe("SUMMARY_NOT_FOUND");
  });

  it("409 document non prêt reste distinct", () => {
    expect(summaryErrorKey("API 409 Conflict: document_not_ready")).toBe("SUMMARY_NOT_READY");
  });
});
