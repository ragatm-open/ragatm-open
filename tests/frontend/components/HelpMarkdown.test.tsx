/**
 * L'aide de niveau 1 n'emploie que des primitives Markdown — mais elle en emploie
 * une qui n'était utilisée nulle part ailleurs : le saut de ligne dur (`\` en fin
 * de ligne), qui place l'exemple SOUS son libellé sans tableau ni HTML.
 *
 * Ce test vérifie que le rendu réel du chat l'honore. Sans lui, l'aide se
 * rendrait en un seul paragraphe compact, et personne ne le verrait avant la prod.
 *
 * Le Markdown ci-dessous est une COPIE de la sortie de `render_help_markdown()`.
 */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AssistantMarkdown } from "@/app/chat/_components/AssistantMarkdown";

const HELP_OVERVIEW = [
  "# Que pouvez-vous faire dans RagATM ?",
  "",
  "Posez votre question normalement : je réponds à partir des documents de l'espace sélectionné, et je cite mes sources.",
  "",
  "- **Poser une question** — je réponds à partir des documents disponibles, en citant mes sources.\\",
  "  `Quels sont les principaux risques du dossier ?`",
  "- **Analyser tout le corpus** — je parcours l’ensemble des documents sur un sujet précis.\\",
  "  `Recherche dans toute la base les informations sur la cybersécurité.`",
  "",
  "## Aller plus loin",
  "",
  "- `/help analyse` — l'analyse globale : modes, progression, couverture",
  "- `/help documents` — questions, recherche et résumés",
  "- `/help sources` — citations et formats de réponse",
  "- `/help limites` — ce que je ne sais pas (encore) faire",
].join("\n");

describe("rendu de l'aide courte", () => {
  it("rend le titre, l'intro et les deux sections", () => {
    render(<AssistantMarkdown content={HELP_OVERVIEW} />);

    expect(
      screen.getByRole("heading", { name: /Que pouvez-vous faire dans RagATM/ }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Aller plus loin" })).toBeInTheDocument();
  });

  it("rend de vraies listes, avec les libellés en gras et les exemples en code", () => {
    const { container } = render(<AssistantMarkdown content={HELP_OVERVIEW} />);

    const items = screen.getAllByRole("listitem");
    expect(items.length).toBe(6); // 2 capacités + 4 sous-commandes

    expect(within(items[0]).getByText("Poser une question").tagName).toBe("STRONG");
    expect(container.querySelectorAll("code").length).toBeGreaterThanOrEqual(6);
  });

  it("place l'exemple SOUS son libellé : le saut de ligne dur est honoré", () => {
    const { container } = render(<AssistantMarkdown content={HELP_OVERVIEW} />);

    // Sans <br>, l'exemple se collerait à la fin de la phrase : l'aide redeviendrait
    // un pavé. C'est le seul point de rendu que cette mise en page suppose.
    const capability = screen.getAllByRole("listitem")[0];
    expect(capability.querySelector("br")).not.toBeNull();
    expect(container.querySelector("table")).toBeNull(); // aucun tableau, jamais
  });

  it("affiche les quatre sous-commandes, qui rendent l'aide navigable", () => {
    render(<AssistantMarkdown content={HELP_OVERVIEW} />);

    for (const topic of ["analyse", "documents", "sources", "limites"]) {
      expect(screen.getByText(`/help ${topic}`)).toBeInTheDocument();
    }
  });
});
