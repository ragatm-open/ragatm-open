/**
 * Le résumé d'un document doit SURVIVRE — à une nouvelle question, à un
 * rechargement de page, et à une réouverture du fil.
 *
 * Ces tests exercent le flux de données réel de `page.tsx`
 * (`GET /threads/:id/messages/` → `mapServerMessages` → `reconcileChatMessages`
 * → rendu) jusqu'au DOM. Ils ne montent pas la page entière : ce qu'ils
 * vérifient, c'est que le résumé arrive jusqu'à l'utilisateur en partant
 * UNIQUEMENT de données persistées — aucun état React ne le porte.
 *
 * C'est le parcours qu'un test E2E couvrirait ; le dépôt n'ayant pas de harnais
 * navigateur, il est joué ici au niveau du flux de données.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SummaryMessageBody } from "@/app/chat/_components/SummaryMessageBody";
import {
  mapServerMessages,
  reconcileChatMessages,
  type WireChatMessage,
} from "@/app/chat/_lib/chatMessages";
import type { Msg } from "@/app/chat/_lib/types";

/** Ce que l'API d'historique renvoie après une demande de résumé. */
const SUMMARY_ROW: WireChatMessage = {
  id: "m-2",
  role: "assistant",
  content: "## Résumé\n\n- Le plan est révisé chaque année.",
  created_at: "2026-07-19T10:00:00Z",
  request_id: "rq-1",
  kind: "document_summary",
  status: "completed",
  document_id: "doc-1",
  summary_id: "sum-1",
};

const ASK_ROW: WireChatMessage = {
  id: "m-1",
  role: "user",
  content: "Résume le document **PCA.pdf**",
  created_at: "2026-07-19T09:59:59Z",
  request_id: "rq-1",
  kind: "document_summary",
  status: "completed",
};

/** Rend un fil comme le fait `page.tsx`, à partir des seuls messages assistant. */
function renderThread(msgs: Msg[]) {
  return render(
    <>
      {msgs
        .filter((m) => m.role === "assistant")
        .map((m, i) => (
          <SummaryMessageBody key={m.id ?? i} m={m} />
        ))}
    </>
  );
}

describe("le résumé survit au cycle complet", () => {
  it("est reconstruit depuis l'historique seul (refresh)", () => {
    // Refresh : aucun état local, uniquement la réponse serveur.
    const msgs = reconcileChatMessages([], mapServerMessages([ASK_ROW, SUMMARY_ROW]));
    renderThread(msgs);

    expect(screen.getByText(/Le plan est révisé chaque année/)).toBeInTheDocument();
  });

  it("reste visible après une nouvelle question", () => {
    const followUp: WireChatMessage = {
      id: "m-4",
      role: "assistant",
      content: "Voici la réponse à votre question.",
      created_at: "2026-07-19T10:05:00Z",
      request_id: "rq-2",
      kind: "",
      status: "",
    };
    const msgs = reconcileChatMessages([], mapServerMessages([ASK_ROW, SUMMARY_ROW, followUp]));
    renderThread(msgs);

    // Les DEUX sont présents, dans l'ordre chronologique.
    expect(screen.getByText(/Le plan est révisé chaque année/)).toBeInTheDocument();
    expect(screen.getByText(/Voici la réponse à votre question/)).toBeInTheDocument();
  });

  it("reste visible après réouverture du fil (remplacement franc)", () => {
    // Changement de fil puis retour : la liste locale est repartie de zéro.
    const other = reconcileChatMessages([], mapServerMessages([]));
    expect(other).toHaveLength(0);

    const back = reconcileChatMessages([], mapServerMessages([ASK_ROW, SUMMARY_ROW]));
    renderThread(back);
    expect(screen.getByText(/Le plan est révisé chaque année/)).toBeInTheDocument();
  });

  it("remplace l'affichage optimiste sans le dupliquer", () => {
    const optimistic: Msg = {
      role: "assistant",
      text: "",
      request_id: "rq-1",
      category: "document_summary",
      status: "pending",
    };
    const msgs = reconcileChatMessages([optimistic], mapServerMessages([ASK_ROW, SUMMARY_ROW]));
    renderThread(msgs);

    expect(screen.getAllByText(/Le plan est révisé chaque année/)).toHaveLength(1);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});

describe("rendu piloté par le statut persisté", () => {
  it("affiche l'état en cours pour un résumé encore en génération", () => {
    // Cas « onglet fermé puis rouvert pendant un résumé long ».
    const msgs = mapServerMessages([
      { ...SUMMARY_ROW, status: "pending", content: "⏳ Résumé en cours de génération…" },
    ]);
    renderThread(msgs);

    expect(screen.getByRole("status")).toHaveTextContent(/en cours de génération/i);
  });

  it("affiche un état d'erreur compréhensible pour un résumé échoué", () => {
    const msgs = mapServerMessages([
      { ...SUMMARY_ROW, status: "failed", content: "La génération du résumé a échoué." },
    ]);
    renderThread(msgs);

    expect(screen.getByRole("alert")).toHaveTextContent(/échoué/i);
  });

  it("rend normalement un ancien message dépourvu des nouveaux champs", () => {
    const legacy: WireChatMessage = {
      id: "old-1",
      role: "assistant",
      content: "Réponse d'avant la migration.",
    };
    renderThread(mapServerMessages([legacy]));

    expect(screen.getByText(/Réponse d'avant la migration/)).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("dégrade vers le rendu standard si le statut est inconnu du frontend", () => {
    const msgs = mapServerMessages([{ ...SUMMARY_ROW, status: "some_future_status" }]);
    renderThread(msgs);

    expect(screen.getByText(/Le plan est révisé chaque année/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
