import { describe, it, expect } from "vitest";
import {
  srcType,
  srcBadgeLabel,
  srcCorpusName,
  srcViewerHref,
  type AnySource,
} from "@/app/chat/_lib/sources";
import { corpusCounts } from "@/lib/corpus";

describe("typage des sources (corpus métier vs privé)", () => {
  it("provenance ABSENTE -> unknown, jamais workspace par défaut", () => {
    // Le repli historique affirmait « Workspace » pour une origine perdue en amont.
    // C'était une affirmation fausse, et invisible : on la nomme désormais.
    const s: AnySource = { rank: 1, document_id: "d1", filename: "f.pdf" } as AnySource;
    expect(srcType(s)).toBe("unknown");
    expect(srcBadgeLabel(s)).toBe("Provenance inconnue");
  });

  it("provenance INVALIDE ou inconnue d'un client futur -> unknown", () => {
    const s = { rank: 1, source_type: "something_new" } as unknown as AnySource;
    expect(srcType(s)).toBe("unknown");
    expect(srcBadgeLabel(s)).toBe("Provenance inconnue");
  });

  it("source privée EXPLICITE -> workspace_document + badge Workspace", () => {
    const s: AnySource = {
      rank: 1,
      source_type: "workspace_document",
      document_id: "d1",
      filename: "f.pdf",
    } as AnySource;
    expect(srcType(s)).toBe("workspace_document");
    expect(srcBadgeLabel(s)).toBe("Workspace");
  });

  it("source privée via le sous-objet `source` (format stream)", () => {
    const s: AnySource = {
      rank: 1,
      source: { source_type: "workspace_document", filename: "f.pdf" },
    } as AnySource;
    expect(srcType(s)).toBe("workspace_document");
    expect(srcBadgeLabel(s)).toBe("Workspace");
  });

  it("source corpus (top-level) -> domain_corpus + badge domaine", () => {
    const s: AnySource = {
      rank: 1,
      source_type: "domain_corpus",
      corpus_name: "Procédures qualité",
      business_domain_name: "Logistique",
    } as AnySource;
    expect(srcType(s)).toBe("domain_corpus");
    expect(srcCorpusName(s)).toBe("Procédures qualité");
    expect(srcBadgeLabel(s)).toBe("Corpus Logistique");
  });

  it("source corpus via le sous-objet `source` (format stream)", () => {
    const s: AnySource = {
      rank: 1,
      source: { source_type: "domain_corpus", corpus_name: "Procédures qualité", business_domain_name: "Logistique" },
    } as AnySource;
    expect(srcType(s)).toBe("domain_corpus");
    expect(srcBadgeLabel(s)).toBe("Corpus Logistique");
  });

  it("corpus sans nom de domaine -> 'Corpus métier'", () => {
    const s: AnySource = { rank: 1, source_type: "domain_corpus" } as AnySource;
    expect(srcBadgeLabel(s)).toBe("Corpus métier");
  });

  it("pas de viewer privé pour une source corpus", () => {
    const s: AnySource = { rank: 1, source_type: "domain_corpus", document_id: "x" } as AnySource;
    expect(srcViewerHref(s)).toBeNull();
  });

  it("viewer privé conservé pour une source workspace", () => {
    const s: AnySource = {
      rank: 1,
      source_type: "workspace_document",
      document_id: "doc-1",
      filename: "f.pdf",
    } as AnySource;
    expect(srcViewerHref(s)).toContain("/doc/doc-1");
  });

  it("provenance inconnue : badge honnête, mais lien viewer CONSERVÉ", () => {
    // Le badge ne doit pas affirmer « Workspace » ; le lien, lui, sert l'historique —
    // une source sans `source_type` vient d'un message antérieur au contrat v2, et
    // `/doc/<id>` vérifie de toute façon l'accès au workspace.
    const s: AnySource = { rank: 1, document_id: "doc-1", filename: "f.pdf" } as AnySource;
    expect(srcBadgeLabel(s)).toBe("Provenance inconnue");
    expect(srcViewerHref(s)).toContain("/doc/doc-1");
  });

  it("les anciennes sources restent affichables (aucune exception)", () => {
    const legacy = [
      {} as AnySource,
      { rank: 1 } as AnySource,
      { rank: 1, source: {} } as AnySource,
    ];
    for (const s of legacy) {
      expect(["workspace_document", "domain_corpus", "unknown"]).toContain(srcType(s));
      expect(typeof srcBadgeLabel(s)).toBe("string");
    }
  });
});

describe("compteurs de documents d'un corpus", () => {
  it("sépare le total des documents réellement interrogeables", () => {
    expect(
      corpusCounts({
        documents_count: 12,
        ready_documents_count: 10,
        processing_documents_count: 0,
        failed_documents_count: 2,
      })
    ).toEqual({ total: 12, ready: 10, processing: 0, failed: 2 });
  });

  it("backend antérieur : `ready` vaut null, aucune mention n'est inventée", () => {
    // Ne JAMAIS dériver `ready` de `total` — c'est la confusion que ce lot supprime.
    expect(corpusCounts({ documents_count: 12 })).toEqual({
      total: 12,
      ready: null,
      processing: 0,
      failed: 0,
    });
  });

  it("valeurs absentes ou non numériques -> zéro, jamais NaN", () => {
    expect(corpusCounts(null)).toEqual({ total: 0, ready: null, processing: 0, failed: 0 });
    expect(
      corpusCounts({ documents_count: Number.NaN } as never)
    ).toEqual({ total: 0, ready: null, processing: 0, failed: 0 });
  });
});
