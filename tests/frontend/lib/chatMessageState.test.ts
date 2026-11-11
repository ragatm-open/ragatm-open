/**
 * Transformations pures de la liste de messages du chat.
 *
 * Ces tests verrouillent le défaut réel : les updaters mutaient le message visé
 * dans un tableau seulement copié en surface. React StrictMode rejoue un updater
 * DEPUIS L'ÉTAT DE BASE ; avec une mutation, le second passage repartait d'un
 * objet déjà modifié et concaténait le delta une deuxième fois.
 *
 * Le test décisif est donc `§ rejeu StrictMode` : deux appels depuis le MÊME
 * état initial doivent rendre exactement le même résultat, et laisser l'état
 * initial intact. Ce n'est PAS de l'idempotence de delta — deux deltas
 * réellement successifs doivent bien se cumuler.
 */
import { describe, expect, it } from "vitest";

import {
  appendAssistantDelta,
  clearAwaitingCorpusChoice,
  newLocalId,
  patchAssistant,
  replaceAssistantText,
  setAssistantKind,
  setAssistantSources,
} from "@/app/chat/_lib/chatMessageState";
import type { Msg, SourceV1 } from "@/app/chat/_lib/types";

const A = "assistant-1";

function baseState(): Msg[] {
  return [
    { role: "user", text: "Question ?", local_id: "user-1" },
    { role: "assistant", text: "", local_id: A },
  ];
}

const SOURCES: SourceV1[] = [
  { rank: 1, filename: "a.pdf" },
  { rank: 2, filename: "b.pdf" },
];

describe("chatMessageState — immuabilité", () => {
  it("appendAssistantDelta ne touche ni le tableau ni le message d'origine", () => {
    const initial = baseState();
    const next = appendAssistantDelta(initial, A, "ABC");

    expect(next).not.toBe(initial);
    expect(next[1]).not.toBe(initial[1]);
    expect(initial[1].text).toBe("");
    expect(next[1].text).toBe("ABC");
    // les messages non visés sont conservés par référence : pas de rendu inutile
    expect(next[0]).toBe(initial[0]);
  });

  it("deux deltas successifs se cumulent bien", () => {
    const s1 = appendAssistantDelta(baseState(), A, "AB");
    const s2 = appendAssistantDelta(s1, A, "CD");
    expect(s2[1].text).toBe("ABCD");
  });

  it("un delta vide ne crée pas de nouvel état", () => {
    const initial = baseState();
    expect(appendAssistantDelta(initial, A, "")).toBe(initial);
  });
});

describe("chatMessageState — rejeu StrictMode", () => {
  it("deux invocations depuis le même état de base donnent le même résultat", () => {
    const initial = baseState();

    const first = appendAssistantDelta(initial, A, "ABC");
    const replay = appendAssistantDelta(initial, A, "ABC");

    expect(first[1].text).toBe("ABC");
    expect(replay[1].text).toBe("ABC");
    expect(initial[1].text).toBe("");
  });

  it("le rejeu ne double pas non plus le texte final ni les sources", () => {
    const streamed = appendAssistantDelta(baseState(), A, "PROSE.\n\nSources: [1], [3]");

    const first = replaceAssistantText(streamed, A, "CANONIQUE");
    const replay = replaceAssistantText(streamed, A, "CANONIQUE");
    expect(first[1].text).toBe("CANONIQUE");
    expect(replay[1].text).toBe("CANONIQUE");
    expect(streamed[1].text).toBe("PROSE.\n\nSources: [1], [3]");

    const s1 = setAssistantSources(streamed, A, SOURCES);
    const s2 = setAssistantSources(streamed, A, SOURCES);
    expect(s1[1].sources).toHaveLength(2);
    expect(s2[1].sources).toHaveLength(2);
    expect(streamed[1].sources).toBeUndefined();
  });
});

describe("chatMessageState — ciblage par identifiant", () => {
  const THREE: Msg[] = [
    { role: "assistant", text: "A", local_id: "a" },
    { role: "user", text: "B", local_id: "b" },
    { role: "assistant", text: "C", local_id: "c" },
  ];

  it("écrit dans le message visé même s'il n'est pas le dernier", () => {
    const next = appendAssistantDelta(THREE, "a", "!");
    expect(next[0].text).toBe("A!");
    expect(next[1]).toBe(THREE[1]);
    expect(next[2]).toBe(THREE[2]);
  });

  it("un identifiant inconnu ne change rien (même référence)", () => {
    expect(appendAssistantDelta(THREE, "inconnu", "!")).toBe(THREE);
    expect(replaceAssistantText(THREE, "inconnu", "x")).toBe(THREE);
    expect(setAssistantSources(THREE, "inconnu", SOURCES)).toBe(THREE);
    expect(setAssistantKind(THREE, "inconnu", "no_answer")).toBe(THREE);
    expect(patchAssistant(THREE, "inconnu", { log_id: "l" })).toBe(THREE);
  });

  it("ne vise jamais un message utilisateur portant le même identifiant", () => {
    const state: Msg[] = [{ role: "user", text: "U", local_id: "x" }];
    expect(appendAssistantDelta(state, "x", "!")).toBe(state);
  });
});

describe("chatMessageState — snapshot final et done sans answer", () => {
  it("le snapshot remplace le texte streamé au lieu de s'y ajouter", () => {
    const streamed = appendAssistantDelta(baseState(), A, "texte provisoire");
    const final = replaceAssistantText(streamed, A, "texte final");
    expect(final[1].text).toBe("texte final");
  });

  it("sans snapshot, le texte accumulé reste en un seul exemplaire", () => {
    // `done` sans `answer` (aide, choix de corpus, garde Excel, listing) :
    // l'appelant ne remplace rien, il ne patche que les métadonnées.
    const streamed = appendAssistantDelta(baseState(), A, "texte streamé");
    const done = patchAssistant(streamed, A, { log_id: "log-1", suggestions: [] });
    expect(done[1].text).toBe("texte streamé");
    expect(done[1].log_id).toBe("log-1");
  });
});

describe("chatMessageState — sources, kind, patch", () => {
  it("setAssistantSources remplace la liste sans partager la référence reçue", () => {
    const input = [...SOURCES];
    const next = setAssistantSources(baseState(), A, input);
    expect(next[1].sources).toEqual(SOURCES);
    expect(next[1].sources).not.toBe(input);
  });

  it("setAssistantKind conserve les autres propriétés", () => {
    const withText = appendAssistantDelta(baseState(), A, "texte");
    const next = setAssistantKind(withText, A, "no_answer");
    expect(next[1].kind).toBe("no_answer");
    expect(next[1].text).toBe("texte");
    expect(withText[1].kind).toBeUndefined();
  });

  it("une valeur identique ne recrée pas d'état", () => {
    const kinded = setAssistantKind(baseState(), A, "no_answer");
    expect(setAssistantKind(kinded, A, "no_answer")).toBe(kinded);
    const texted = replaceAssistantText(baseState(), A, "x");
    expect(replaceAssistantText(texted, A, "x")).toBe(texted);
  });

  it("patchAssistant fusionne sans muter", () => {
    const initial = baseState();
    const next = patchAssistant(initial, A, { log_id: "l1", analysis_id: "an-1" });
    expect(next[1].log_id).toBe("l1");
    expect(next[1].analysis_id).toBe("an-1");
    expect(initial[1].log_id).toBeUndefined();
  });
});

describe("chatMessageState — attente de choix de corpus", () => {
  it("retire l'attente partout sans muter les messages", () => {
    const initial: Msg[] = [
      { role: "assistant", text: "A", local_id: "a", awaiting_corpus_choice: true },
      { role: "assistant", text: "B", local_id: "b" },
    ];
    const next = clearAwaitingCorpusChoice(initial);
    expect(next[0].awaiting_corpus_choice).toBe(false);
    expect(initial[0].awaiting_corpus_choice).toBe(true);
    // message déjà conforme : conservé par référence
    expect(next[1]).toBe(initial[1]);
  });

  it("ne recrée rien quand aucun message n'attend de choix", () => {
    const initial = baseState();
    expect(clearAwaitingCorpusChoice(initial)).toBe(initial);
  });
});

describe("chatMessageState — identifiants locaux", () => {
  it("newLocalId rend des valeurs distinctes et non vides", () => {
    const ids = new Set(Array.from({ length: 50 }, () => newLocalId()));
    expect(ids.size).toBe(50);
    for (const id of ids) expect(id.length).toBeGreaterThan(0);
  });
});
