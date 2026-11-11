// « Citées » ≠ « consultées » : le badge vert n'a le droit d'affirmer que ce que le modèle a
// réellement cité. Régression fondatrice : « Je ne sais pas. » affichait « 8 sources citées ✓ ».
//
// L'autorité est le contrat serveur ; les heuristiques de texte ne servent qu'aux messages
// relus depuis l'historique (persistés avant l'existence du contrat).
import { describe, it, expect } from "vitest";
import {
  looksLikeAbstention,
  parseCitedSourceIds,
  resolveSourcesDisplay,
  type SourcesContract,
} from "@/app/chat/_lib/citations";

const contract = (over: Partial<SourcesContract>): SourcesContract => ({
  retrieved_sources_count: 8,
  prompt_sources_count: 8,
  cited_source_ids: [],
  answer_is_abstention: false,
  ...over,
});

describe("parseCitedSourceIds", () => {
  it("dédoublonne, trie et borne au nombre de sources", () => {
    expect(parseCitedSourceIds("A [3], B [1], C [3], hallucination [12]", 8)).toEqual([1, 3]);
  });

  it("ne rend rien sans source affichée", () => {
    expect(parseCitedSourceIds("Réponse [1]", 0)).toEqual([]);
  });
});

describe("looksLikeAbstention (repli historique)", () => {
  it.each([
    "Je ne sais pas.",
    "Je ne sais pas.\n\nSources: [1], [2]",
    "Les sources ne permettent pas de répondre.",
    "Je n’ai pas assez d’informations.",
    "I don't know.",
    "",
  ])("détecte %j", (text) => {
    expect(looksLikeAbstention(text)).toBe(true);
  });

  it("ne classe pas une réponse substantielle comme abstention", () => {
    expect(looksLikeAbstention("Valteria publie un reporting mensuel [2].")).toBe(false);
  });
});

describe("resolveSourcesDisplay — contrat serveur (autorité)", () => {
  it("abstention : sources consultées, jamais citées", () => {
    const d = resolveSourcesDisplay({
      contract: contract({ answer_is_abstention: true }),
      text: "Je ne sais pas.",
      sourcesCount: 8,
    });
    expect(d.kind).toBe("consulted");
    expect(d.ids).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
  });

  it("citations réelles : seules les pastilles citées", () => {
    const d = resolveSourcesDisplay({
      contract: contract({ cited_source_ids: [2, 5] }),
      text: "…[2]…[5]",
      sourcesCount: 8,
    });
    expect(d.kind).toBe("cited");
    expect(d.ids).toEqual([2, 5]);
  });

  it("réponse sans citation : consultées, pas de badge positif", () => {
    const d = resolveSourcesDisplay({
      contract: contract({ cited_source_ids: [] }),
      text: "Le dispositif repose sur trois piliers.",
      sourcesCount: 3,
    });
    expect(d.kind).toBe("consulted");
  });

  it("écarte une citation hors bornes annoncée par le serveur", () => {
    const d = resolveSourcesDisplay({
      contract: contract({ cited_source_ids: [12] }),
      text: "…[12]",
      sourcesCount: 8,
    });
    expect(d.kind).toBe("consulted");
  });
});

describe("resolveSourcesDisplay — messages d'historique (sans contrat)", () => {
  it("un ancien message d'abstention n'affiche plus de badge de citation", () => {
    const d = resolveSourcesDisplay({
      contract: null,
      text: "Je ne sais pas.\n\nSources: [1], [2], [3]",
      sourcesCount: 3,
    });
    expect(d.kind).toBe("consulted");
  });

  it("retombe sur les [n] présents dans le texte", () => {
    const d = resolveSourcesDisplay({
      contract: null,
      text: "Paris est la capitale [1].\n\nSources: [1]",
      sourcesCount: 4,
    });
    expect(d.kind).toBe("cited");
    expect(d.ids).toEqual([1]);
  });
});
