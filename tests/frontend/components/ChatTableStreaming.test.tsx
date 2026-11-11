/**
 * Bout-en-bout (hors LLM/DB) : trames SSE réelles → parseur réel → accumulation
 * telle que page.tsx → AssistantMarkdown → DOM.
 *
 * Garantit l'invariant central du lot : un tableau Markdown GFM émis token par
 * token par le backend arrive bien dans le DOM sous forme de vrai <table>.
 */
import React from "react";
import { render } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { readSseStream } from "@/app/chat/_lib/sse";
import * as auth from "@/lib/auth";
import { AssistantMarkdown } from "@/app/chat/_components/AssistantMarkdown";

/** Réplique exacte de backend/api/v1/chat/views.py::_sse (json.dumps + \n\n). */
function sseFrame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

function sseResponse(chunks: string[]) {
  const enc = new TextEncoder();
  let i = 0;
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    text: async () => chunks.join(""),
    body: {
      getReader() {
        return {
          read: async () => {
            if (i < chunks.length) {
              const value = enc.encode(chunks[i]);
              i += 1;
              return { value, done: false };
            }
            return { value: undefined, done: true };
          },
          cancel: async () => {},
          releaseLock: () => {},
        };
      },
    },
  } as unknown as Response;
}

// Réponse typique du prompt news en mode tableau (5 lignes max, ordre récent → ancien).
const LLM_TABLE_ANSWER = [
  "| Date | Résumé | Source |",
  "|---|---|---|",
  "| 2026-06-12 | UniCredit relève ses objectifs annuels. | rapport.pdf, p. 24 |",
  "| 2026-05-03 | Cession d'une participation minoritaire. | note\\|interne.pdf, p. 8 |",
].join("\n");

describe("Chat — tableau streamé de bout en bout", () => {
  beforeEach(() => {
    vi.spyOn(auth, "getCsrfToken").mockReturnValue(null);
  });

  it("rend un vrai <table> à partir de trames SSE token par token", async () => {
    // Le backend découpe la réponse en deltas ; les \n sont échappés par json.dumps.
    const deltas = LLM_TABLE_ANSWER.match(/[\s\S]{1,9}/g) ?? [];
    const frames = [
      sseFrame("meta", { request_id: "r1", thread_id: "t1", log_id: "l1" }),
      sseFrame("sources", { sources: [] }),
      ...deltas.map((delta) => sseFrame("token", { delta })),
      sseFrame("done", { request_id: "r1", status: "ok" }),
    ];

    vi.stubGlobal(
      "fetch",
      vi.fn(async () => sseResponse(frames))
    );

    // Accumulation identique à page.tsx : last.text = (last.text || "") + delta
    let text = "";
    let doneSeen = false;
    await readSseStream({
      url: "/api/v1/chat/stream",
      body: {},
      onEvent: (event, data: any) => {
        if (event === "token") text = (text || "") + data.delta;
        if (event === "done") doneSeen = true;
      },
    });

    expect(doneSeen).toBe(true);
    // Le transport n'a perdu aucun saut de ligne.
    expect(text).toBe(LLM_TABLE_ANSWER);

    const { container } = render(<AssistantMarkdown content={text} />);

    expect(container.querySelectorAll("table")).toHaveLength(1);
    expect(container.querySelectorAll("th")).toHaveLength(3);
    expect(container.querySelectorAll("tbody tr")).toHaveLength(2);

    const headers = Array.from(container.querySelectorAll("th")).map((th) => th.textContent);
    expect(headers).toEqual(["Date", "Résumé", "Source"]);

    // Ordre chronologique inverse conservé.
    const firstRow = container.querySelectorAll("tbody tr")[0];
    expect(firstRow.textContent).toContain("2026-06-12");

    // Le pipe échappé par le backend ne crée pas de colonne fantôme.
    const secondRow = container.querySelectorAll("tbody tr")[1];
    expect(secondRow.querySelectorAll("td")).toHaveLength(3);
    expect(secondRow.textContent).toContain("note|interne.pdf, p. 8");

    // Aucun pipe brut ni ligne de séparation visible.
    expect(container.textContent).not.toContain("|---|");
    expect(container.textContent).not.toContain("| Date |");

    // Colonnes reconnues → ajustements visuels appliqués.
    expect(container.querySelector('th[data-col="date"]')).not.toBeNull();
    expect(container.querySelector('td[data-col="source"]')).not.toBeNull();
  });
});
