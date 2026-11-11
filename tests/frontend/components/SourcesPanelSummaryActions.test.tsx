/**
 * Actions « Résumer » du panneau de sources — contrat de PROVENANCE.
 *
 * Le panneau est le dernier composant qui connaît l'origine d'une source. C'est
 * donc lui qui doit émettre une référence DISCRIMINÉE : jusqu'ici il transmettait
 * la source brute et l'appelant renvoyait l'UUID nu vers l'API privée, quelle que
 * soit sa provenance.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SourcesPanel } from "@/app/chat/_components/SourcesPanel";
import { srcReference } from "@/app/chat/_lib/sources";
import type { Msg, SourceV1 } from "@/app/chat/_lib/types";

const PRIVATE_SOURCE: SourceV1 = {
  rank: 1,
  schema_version: 3,
  source_type: "workspace_document",
  document_id: "d-priv",
  chunk_id: "c-priv",
  filename: "note.pdf",
  page_number: 4,
  snippet: "Note interne.",
  meta: {},
} as SourceV1;

const CORPUS_SOURCE: SourceV1 = {
  rank: 1,
  schema_version: 3,
  source_type: "domain_corpus",
  document_id: "d-corpus",
  chunk_id: "c-corpus",
  filename: "jrc_vc_europe.pdf",
  page_number: 13,
  snippet: "Définition de référence.",
  corpus_id: "corp-1",
  corpus_name: "VC Reference Corpus",
  business_domain_name: "Venture Capital",
  meta: {},
} as SourceV1;

const LEGACY_SOURCE: SourceV1 = {
  rank: 1,
  schema_version: 3,
  document_id: "d-old",
  chunk_id: "c-old",
  filename: "ancien.pdf",
  page_number: 2,
  snippet: "Message antérieur au contrat v2.",
  meta: {},
};

const NO_DOC_SOURCE: SourceV1 = {
  rank: 1,
  schema_version: 3,
  chunk_id: "c-orphan",
  filename: "orphelin.pdf",
  page_number: 1,
  snippet: "Source sans document.",
  meta: {},
} as SourceV1;

function panel(sources: SourceV1[], onSummarize = vi.fn(), busy = false) {
  const msg = { role: "assistant", text: "…", sources } as Msg;
  render(
    <SourcesPanel
      msg={msg}
      activeN={null}
      onCite={vi.fn()}
      onSummarize={onSummarize}
      busy={busy}
    />,
  );
  return onSummarize;
}

describe("SourcesPanel — actions de résumé et provenance", () => {
  it("source privée : le clic transmet `workspace_document`", async () => {
    const onSummarize = panel([PRIVATE_SOURCE]);

    await userEvent.click(screen.getByText("Résumer le document"));

    expect(onSummarize).toHaveBeenCalledWith(
      expect.objectContaining({ sourceType: "workspace_document", documentId: "d-priv" }),
      "document",
    );
  });

  it("source corpus : le clic transmet `domain_corpus` et le corpus", async () => {
    const onSummarize = panel([CORPUS_SOURCE]);

    await userEvent.click(screen.getByText("Résumer le document"));

    expect(onSummarize).toHaveBeenCalledWith(
      expect.objectContaining({
        sourceType: "domain_corpus",
        documentId: "d-corpus",
        corpusId: "corp-1",
      }),
      "document",
    );
  });

  it("résumé de PAGE d'une source corpus : scope page + provenance", async () => {
    const onSummarize = panel([CORPUS_SOURCE]);

    await userEvent.click(screen.getByText("Résumer la page 13"));

    expect(onSummarize).toHaveBeenCalledWith(
      expect.objectContaining({
        sourceType: "domain_corpus",
        documentId: "d-corpus",
        pageNumber: 13,
      }),
      "page",
    );
  });

  it("résumé de PAGE d'une source privée : comportement inchangé", async () => {
    const onSummarize = panel([PRIVATE_SOURCE]);

    await userEvent.click(screen.getByText("Résumer la page 4"));

    expect(onSummarize).toHaveBeenCalledWith(
      expect.objectContaining({ sourceType: "workspace_document", pageNumber: 4 }),
      "page",
    );
  });

  it("source SANS provenance : `unknown`, actions conservées", async () => {
    // Compatibilité historique : ces messages doivent garder leurs boutons, et
    // emprunter le chemin privé (cf. summaryEndpointFor).
    const onSummarize = panel([LEGACY_SOURCE]);

    await userEvent.click(screen.getByText("Résumer le document"));

    expect(onSummarize).toHaveBeenCalledWith(
      expect.objectContaining({ sourceType: "unknown", documentId: "d-old" }),
      "document",
    );
  });

  it("source sans document_id : aucune action proposée", () => {
    panel([NO_DOC_SOURCE]);

    expect(screen.queryByText("Résumer le document")).not.toBeInTheDocument();
    expect(screen.queryByText(/Résumer la page/)).not.toBeInTheDocument();
  });

  it("une source corpus SANS page n'affiche pas l'action de page", () => {
    panel([{ ...CORPUS_SOURCE, page_number: null } as SourceV1]);

    expect(screen.getByText("Résumer le document")).toBeInTheDocument();
    expect(screen.queryByText(/Résumer la page/)).not.toBeInTheDocument();
  });

  it("busy : les actions sont désactivées (pas de double génération)", () => {
    panel([CORPUS_SOURCE], vi.fn(), true);

    expect(screen.getByText("Résumer le document").closest("button")).toBeDisabled();
  });

  it("le badge corpus et l'absence de viewer privé sont préservés", () => {
    const { container } = render(
      <SourcesPanel
        msg={{ role: "assistant", text: "…", sources: [CORPUS_SOURCE] } as Msg}
        activeN={null}
        onCite={vi.fn()}
        onSummarize={vi.fn()}
        busy={false}
      />,
    );

    expect(screen.getByText(/Corpus Venture Capital/)).toBeInTheDocument();
    expect(container.textContent).toContain("VC Reference Corpus");
    // Une source de corpus n'a pas de viewer privé : ce comportement ne change pas.
    expect(screen.queryByText("Ouvrir le document")).not.toBeInTheDocument();
  });
});

describe("srcReference", () => {
  it("dérive la provenance et la page", () => {
    expect(srcReference(CORPUS_SOURCE as never)).toEqual({
      sourceType: "domain_corpus",
      documentId: "d-corpus",
      pageNumber: 13,
      corpusId: "corp-1",
    });
  });

  it("lit aussi le format `source` imbriqué (flux SSE)", () => {
    const nested = {
      rank: 1,
      source: {
        source_type: "domain_corpus",
        document_id: "d-nested",
        page_number: 7,
        corpus_id: "corp-2",
      },
    };
    expect(srcReference(nested as never)).toEqual({
      sourceType: "domain_corpus",
      documentId: "d-nested",
      pageNumber: 7,
      corpusId: "corp-2",
    });
  });

  it("renvoie null sans identifiant de document", () => {
    expect(srcReference(NO_DOC_SOURCE as never)).toBeNull();
  });
});
