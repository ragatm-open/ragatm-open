/**
 * La couture entre le bouton et le backend.
 *
 * Le contrat vérifié ici est exactement celui que les tests HTTP du backend
 * exercent de l'autre côté (`question: ""` + `corpus_choice`) : les deux moitiés
 * du Lot 3 se rejoignent sur ce corps de requête.
 */
import { describe, expect, it } from "vitest";

import { buildChatRequestBody } from "@/app/chat/_lib/chatRequest";

describe("corps de la requête de chat", () => {
  it("envoie une question ordinaire telle quelle, sans choix", () => {
    const body = buildChatRequestBody({
      ragSpaceId: "s-1",
      question: "Quels sont les risques ?",
      threadId: "t-1",
    });

    expect(body).toEqual({
      rag_space_id: "s-1",
      question: "Quels sont les risques ?",
      thread_id: "t-1",
    });
    expect(body).not.toHaveProperty("corpus_choice");
  });

  it.each(["quick_response", "global_analysis"] as const)(
    "envoie %s comme identifiant d'action, et AUCUN texte à interpréter",
    (choice) => {
      const body = buildChatRequestBody({
        ragSpaceId: "s-1",
        // Même si un libellé traîne, il ne doit pas partir : le serveur écrit le sien.
        question: "Analyse complète",
        threadId: "t-1",
        corpusChoice: choice,
      });

      expect(body.corpus_choice).toBe(choice);
      expect(body.question).toBe("");
      // La question d'origine n'est jamais retransmise par le navigateur : c'est le
      // serveur qui la retrouve. Sinon, n'importe qui pourrait la remplacer.
      expect(JSON.stringify(body)).not.toContain("risques");
    },
  );

  it("normalise l'absence de fil en null", () => {
    expect(buildChatRequestBody({ ragSpaceId: "s-1", question: "x" }).thread_id).toBeNull();
    expect(
      buildChatRequestBody({ ragSpaceId: "s-1", question: "x", threadId: "" }).thread_id,
    ).toBeNull();
  });

  describe("langue de réponse", () => {
    it.each(["auto", "fr", "en"] as const)("transmet la préférence %s", (lang) => {
      const body = buildChatRequestBody({ ragSpaceId: "s-1", question: "x", answerLanguage: lang });
      expect(body.answer_language).toBe(lang);
    });

    it("omet le champ quand la préférence est inconnue", () => {
      // Champ absent => le backend retombe sur `preferred_answer_language` du profil.
      // Envoyer `null` ou `undefined` explicitement ferait échouer le serializer.
      expect(buildChatRequestBody({ ragSpaceId: "s-1", question: "x" })).not.toHaveProperty(
        "answer_language",
      );
      expect(
        buildChatRequestBody({ ragSpaceId: "s-1", question: "x", answerLanguage: null }),
      ).not.toHaveProperty("answer_language");
    });

    it("n'émet jamais une valeur hors contrat", () => {
      const body = buildChatRequestBody({
        ragSpaceId: "s-1",
        question: "x",
        // @ts-expect-error valeur volontairement invalide (donnée corrompue côté client)
        answerLanguage: "de",
      });
      expect(body).not.toHaveProperty("answer_language");
    });
  });
});
