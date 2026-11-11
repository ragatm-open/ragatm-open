import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { SourcesPanel } from "@/app/chat/_components/SourcesPanel";
import type { Msg, SourceV1 } from "@/app/chat/_lib/types";

/**
 * rendu minimal d'un repère de provenance générique.
 *
 * L2 : le libellé humain du locator s'affiche (chemin + identifiant, chemin seul,
 * identifiant seul), sans pastille de page. L3 : une source ancien contrat est rendue
 * exactement comme avant. L4 : un PDF garde sa page. Le texte du locator est une donnée
 * (échappement React) : jamais interprété comme du HTML.
 */

const STRUCT: SourceV1 = {
  rank: 1,
  schema_version: 3,
  document_id: "d-struct",
  chunk_id: "c-1",
  filename: "projet.dat",
  page_number: null,
  snippet: "Clause sept : dalle armée HA12.",
  meta: { locator: { type: "element", path: "/projet/lot[3]/clause[7]", identifier: "CL-0042" } },
};

const PDF: SourceV1 = {
  rank: 2,
  schema_version: 3,
  document_id: "d-pdf",
  chunk_id: "c-2",
  filename: "Rapport.pdf",
  page_number: 12,
  snippet: "Hausse constatée.",
  meta: {},
};

function panel(sources: SourceV1[]) {
  const msg = { role: "assistant", text: "…", sources } as Msg;
  return render(
    <SourcesPanel msg={msg} activeN={null} onCite={vi.fn()} onSummarize={vi.fn()} busy={false} />,
  );
}

describe("SourcesPanel — repère de provenance générique (L2)", () => {
  it("affiche chemin + identifiant, sans pastille de page", () => {
    const { container } = panel([STRUCT]);
    expect(screen.getByText("projet.dat")).toBeInTheDocument();
    expect(screen.getByText("/projet/lot[3]/clause[7] · CL-0042")).toBeInTheDocument();
    expect(screen.queryByText(/^p\. \d+$/)).toBeNull();
    expect(container.textContent).not.toMatch(/Page\s*(null|0)/i);
    expect(container.textContent).not.toContain("locator");
    expect(container.textContent).not.toContain("element");
  });

  it("chemin seul", () => {
    panel([{ ...STRUCT, meta: { locator: { path: "/projet/lot[3]" } } }]);
    expect(screen.getByText("/projet/lot[3]")).toBeInTheDocument();
  });

  it("identifiant seul", () => {
    panel([{ ...STRUCT, meta: { locator: { identifier: "CL-0042" } } }]);
    expect(screen.getByText("CL-0042")).toBeInTheDocument();
  });

  it("locator nul ou malformé : le fichier reste affiché, rien n'est inventé, aucun crash", () => {
    const { container } = panel([
      { ...STRUCT, meta: { locator: null } },
      { ...STRUCT, rank: 2, chunk_id: "c-9", meta: { locator: "n'importe quoi" as unknown as null } },
      { ...STRUCT, rank: 3, chunk_id: "c-10", meta: { locator: { path: 12 as unknown as string } } },
    ]);
    expect(screen.getAllByText("projet.dat")).toHaveLength(3);
    expect(container.textContent).not.toContain("·");
    expect(container.textContent).not.toMatch(/undefined|null|\[object/);
  });

  it("le libellé est une donnée échappée, jamais du HTML interprété", () => {
    const { container } = panel([
      { ...STRUCT, meta: { locator: { path: "<img src=x onerror=alert(1)>", identifier: "<b>gras</b>" } } },
    ]);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(container.textContent).toContain("<img src=x onerror=alert(1)> · <b>gras</b>");
  });

  it("le lien d'ouverture ne contient pas page=", () => {
    panel([STRUCT]);
    const href = screen.getByRole("link", { name: /Ouvrir le document/i }).getAttribute("href") || "";
    expect(href).toContain("/doc/d-struct");
    expect(href).not.toContain("page=");
  });
});

describe("SourcesPanel — non-régression (L3, L4)", () => {
  it("L3 : ancien contrat (aucun meta) : rendu inchangé, aucun repère", () => {
    const legacy = {
      rank: 1,
      schema_version: 2,
      document_id: "d",
      chunk_id: "c",
      filename: "vieux.pdf",
      page_number: 3,
      snippet: "…",
    } as SourceV1;
    const { container } = panel([legacy]);
    expect(screen.getByText("vieux.pdf")).toBeInTheDocument();
    expect(screen.getByText("p. 3")).toBeInTheDocument();
    expect(container.textContent).not.toContain("·");
  });

  it("L4 : un PDF garde sa page et son lien page=", () => {
    panel([PDF]);
    expect(screen.getByText("p. 12")).toBeInTheDocument();
    const href = screen.getByRole("link", { name: /Ouvrir le document/i }).getAttribute("href") || "";
    expect(href).toContain("page=12");
  });
});

describe("SourcesPanel — passage de transcription audio", () => {
  const AUDIO_SRC: SourceV1 = {
    ...STRUCT,
    document_id: "d-audio",
    filename: "reunion.mp3",
    meta: {
      source_is_asr: true,
      timestamp_precision: "segment",
      locator: { type: "time_range", identifier: "00:12:35 → 00:13:48" },
    },
  };

  it("libellé « Transcription audio · intervalle », rendu en texte et non en lien", () => {
    panel([AUDIO_SRC]);
    const label = screen.getByText("Transcription audio · 00:12:35 → 00:13:48");
    expect(label.closest("a")).toBeNull();
    expect(screen.queryByText(/horodatage approximatif/)).toBeNull();
  });

  it("précision à la partie : mention « horodatage approximatif »", () => {
    panel([{ ...AUDIO_SRC, meta: { ...AUDIO_SRC.meta, timestamp_precision: "part" } }]);
    expect(
      screen.getByText("Transcription audio · 00:12:35 → 00:13:48 (horodatage approximatif)")
    ).toBeInTheDocument();
  });
});
