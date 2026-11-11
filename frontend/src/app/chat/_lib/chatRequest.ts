/**
 * Corps de la requête de chat — le point où un CLIC devient une action serveur.
 *
 * Isolé ici pour une raison précise : c'est la couture entre le bouton et le
 * backend, et c'est exactement là qu'une régression serait invisible. Un choix doit
 * partir comme un identifiant d'action, JAMAIS comme du texte à réinterpréter.
 */
import type { AnswerLanguage } from "@/lib/api";

import type { CorpusChoice } from "./types";

export type ChatRequestBody = {
  rag_space_id: string;
  question: string;
  thread_id: string | null;
  corpus_choice?: CorpusChoice;
  answer_language?: AnswerLanguage;
};

const ANSWER_LANGUAGES: readonly AnswerLanguage[] = ["auto", "fr", "en"];

export function buildChatRequestBody({
  ragSpaceId,
  question,
  threadId,
  corpusChoice,
  answerLanguage,
}: {
  ragSpaceId: string;
  question: string;
  threadId?: string | null;
  corpusChoice?: CorpusChoice | null;
  answerLanguage?: AnswerLanguage | null;
}): ChatRequestBody {
  const body: ChatRequestBody = {
    rag_space_id: ragSpaceId,
    // Sur un choix, le texte visible n'a aucune valeur d'autorité : c'est le serveur
    // qui écrira le libellé persisté. On n'envoie donc rien à interpréter.
    question: corpusChoice ? "" : question,
    thread_id: threadId || null,
  };
  if (corpusChoice) body.corpus_choice = corpusChoice;
  // Émis seulement si on connaît réellement la préférence : un champ absent laisse
  // le backend retomber sur `preferred_answer_language` du profil (repli conservé,
  // cf. chat_service). Une valeur inconnue n'est jamais transmise telle quelle —
  // le serializer la rejetterait en 400 et ferait échouer tout le tour de chat.
  if (answerLanguage && ANSWER_LANGUAGES.includes(answerLanguage)) {
    body.answer_language = answerLanguage;
  }
  return body;
}
