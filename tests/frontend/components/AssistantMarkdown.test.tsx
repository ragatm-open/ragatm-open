import React from "react";
import { readFileSync } from "node:fs";
import path from "node:path";
import { render, screen } from "@testing-library/react";
import { describe, it, expect, beforeEach } from "vitest";
import { AssistantMarkdown } from "@/app/chat/_components/AssistantMarkdown";

const COMPONENT_PATH = path.resolve(__dirname, "../../../frontend/src/app/chat/_components/AssistantMarkdown.tsx");
const CSS_PATH = path.resolve(__dirname, "../../../frontend/src/app/chat/_components/AssistantMarkdown.module.css");

const VALID_TABLE = [
  "| Date | Résumé | Source |",
  "|---|---|---|",
  "| 2026-06-12 | Exemple | document.pdf, p. 24 |",
].join("\n");

describe("AssistantMarkdown — F1 tableau GFM valide", () => {
  it("rend un vrai <table> avec thead/tbody/th/td", () => {
    const { container } = render(<AssistantMarkdown content={VALID_TABLE} />);

    expect(container.querySelectorAll("table")).toHaveLength(1);
    expect(container.querySelectorAll("thead")).toHaveLength(1);
    expect(container.querySelectorAll("tbody")).toHaveLength(1);

    const th = container.querySelectorAll("th");
    const td = container.querySelectorAll("td");
    expect(th).toHaveLength(3);
    expect(td).toHaveLength(3);

    th.forEach((cell) => expect(cell.getAttribute("scope")).toBe("col"));
    expect(td[2].textContent).toBe("document.pdf, p. 24");
  });
});

describe("AssistantMarkdown — F2 texte avant et après", () => {
  it("conserve l'ordre paragraphe / table / paragraphe", () => {
    const content = `Voici les résultats :\n\n${VALID_TABLE}\n\nSources : [1]`;
    const { container } = render(<AssistantMarkdown content={content} />);

    expect(container.querySelectorAll("table")).toHaveLength(1);

    const blocks = Array.from(container.querySelectorAll("p, table"));
    expect(blocks.map((b) => b.tagName.toLowerCase())).toEqual(["p", "table", "p"]);
    expect(blocks[0].textContent).toContain("Voici les résultats");
    expect(blocks[2].textContent).toContain("Sources : [1]");
  });
});

describe("AssistantMarkdown — F3 pipe échappé", () => {
  it("garde 2 colonnes et restitue le pipe littéral", () => {
    const content = ["| Élément | Valeur |", "|---|---|", "| Choix | A \\| B |"].join("\n");
    const { container } = render(<AssistantMarkdown content={content} />);

    expect(container.querySelectorAll("th")).toHaveLength(2);

    const cells = container.querySelectorAll("tbody td");
    expect(cells).toHaveLength(2);
    expect(cells[1].textContent).toBe("A | B");
  });
});

describe("AssistantMarkdown — F4 tableau invalide", () => {
  it("ne crashe pas, ne produit pas de <table>, garde le contenu visible", () => {
    const content = ["| Date | Résumé | Source |", "| 2026-06-12 | Exemple | document.pdf |"].join("\n");
    const { container } = render(<AssistantMarkdown content={content} />);

    expect(container.querySelectorAll("table")).toHaveLength(0);
    // Dégradation visible et assumée : le contenu reste lisible en texte.
    expect(container.textContent).toContain("2026-06-12");
    expect(container.textContent).toContain("document.pdf");
  });
});

describe("AssistantMarkdown — F5 streaming vs injection directe", () => {
  it("produit un DOM final identique", () => {
    const direct = render(<AssistantMarkdown content={VALID_TABLE} />);
    const directHtml = direct.container.innerHTML;
    direct.unmount();

    // Reproduit l'accumulation SSE (page.tsx: last.text += delta).
    const deltas = VALID_TABLE.match(/[\s\S]{1,5}/g) ?? [];
    let acc = "";
    const streamed = render(<AssistantMarkdown content={acc} />);
    for (const delta of deltas) {
      acc += delta;
      streamed.rerender(<AssistantMarkdown content={acc} />);
    }

    expect(streamed.container.innerHTML).toBe(directHtml);
    expect(streamed.container.querySelectorAll("table")).toHaveLength(1);
  });
});

describe("AssistantMarkdown — F6 états intermédiaires", () => {
  const fragments = ["| Date |", "| Date | Résumé", "| Date | Résumé |", "| Date | Résumé |\n|---|---"];

  it.each(fragments)("ne crashe pas sur un fragment partiel: %s", (fragment) => {
    const { container } = render(<AssistantMarkdown content={fragment} />);
    expect(container.querySelectorAll("script")).toHaveLength(0);
    expect(container.textContent).toContain("Date");
  });

  it("aboutit au bon rendu quand le contenu complet arrive", () => {
    const view = render(<AssistantMarkdown content={fragments[3]} />);
    view.rerender(<AssistantMarkdown content={VALID_TABLE} />);
    expect(view.container.querySelectorAll("table")).toHaveLength(1);
    expect(view.container.querySelectorAll("td")).toHaveLength(3);
  });
});

describe("AssistantMarkdown — F7/F8/F9/F10 non-régression Markdown", () => {
  it("F7 — gras", () => {
    const { container } = render(<AssistantMarkdown content="Texte **important** ici." />);
    const strong = container.querySelector("strong");
    expect(strong).not.toBeNull();
    expect(strong?.textContent).toBe("important");
  });

  it("F8 — deux paragraphes distincts", () => {
    const { container } = render(<AssistantMarkdown content={"Premier paragraphe.\n\nSecond paragraphe."} />);
    const ps = container.querySelectorAll("p");
    expect(ps).toHaveLength(2);
    expect(ps[0].textContent).toBe("Premier paragraphe.");
    expect(ps[1].textContent).toBe("Second paragraphe.");
  });

  it("F9 — listes à puces et numérotées", () => {
    const content = "- un\n- deux\n\n1. premier\n2. second";
    const { container } = render(<AssistantMarkdown content={content} />);

    expect(container.querySelectorAll("ul")).toHaveLength(1);
    expect(container.querySelectorAll("ol")).toHaveLength(1);
    expect(container.querySelectorAll("ul li")).toHaveLength(2);
    expect(container.querySelectorAll("ol li")).toHaveLength(2);
  });

  it("F9bis — citation", () => {
    const { container } = render(<AssistantMarkdown content="> une citation" />);
    expect(container.querySelector("blockquote")?.textContent).toContain("une citation");
  });

  it("F10 — code inline et bloc, contenu textuel non exécuté", () => {
    const content = "Utilise `npm run build`.\n\n```js\nconst a = 1;\n```";
    const { container } = render(<AssistantMarkdown content={content} />);

    const codes = container.querySelectorAll("code");
    expect(codes.length).toBeGreaterThanOrEqual(2);
    expect(container.querySelector("pre")).not.toBeNull();
    expect(container.textContent).toContain("npm run build");
    expect(container.textContent).toContain("const a = 1;");
    expect(container.querySelectorAll("script")).toHaveLength(0);
  });
});

describe("AssistantMarkdown — F11 XSS HTML brut", () => {
  beforeEach(() => {
    delete (window as any).__xss;
  });

  it("n'interprète aucun HTML brut issu du LLM", () => {
    const content = '<script>window.__xss = true</script>\n<img src=x onerror="window.__xss = true">';
    const { container } = render(<AssistantMarkdown content={content} />);

    expect(container.querySelectorAll("script")).toHaveLength(0);
    expect(container.querySelectorAll("img")).toHaveLength(0);
    expect(container.querySelectorAll("iframe")).toHaveLength(0);
    expect(container.querySelector("[onerror]")).toBeNull();
    expect(container.querySelectorAll("*").length).toBe(1); // uniquement le div racine

    // Le HTML brut est rendu en texte échappé : dégradation visible, jamais exécutée.
    expect(container.innerHTML).toContain("&lt;script&gt;");
    expect(container.innerHTML).toContain("&lt;img");
    expect((window as any).__xss).toBeUndefined();
  });
});

describe("AssistantMarkdown — F12 liens", () => {
  it("neutralise javascript: et data:, rend le lien https en sécurité", () => {
    const content = [
      "[attaque](javascript:alert(1))",
      "",
      "[data](data:text/html,evil)",
      "",
      "[sûr](https://example.com)",
    ].join("\n");
    const { container } = render(<AssistantMarkdown content={content} />);

    const anchors = Array.from(container.querySelectorAll("a"));
    // Seul le lien https reste cliquable.
    expect(anchors).toHaveLength(1);

    const safe = anchors[0];
    expect(safe.getAttribute("href")).toBe("https://example.com");
    expect(safe.getAttribute("target")).toBe("_blank");
    expect(safe.getAttribute("rel")).toBe("noopener noreferrer");

    // Les libellés dangereux restent visibles mais non cliquables.
    expect(container.textContent).toContain("attaque");
    expect(container.textContent).toContain("data");
    expect(container.innerHTML).not.toContain("javascript:");
    expect(container.innerHTML).not.toContain("data:text/html");
  });

  it("garde un lien relatif dans le même onglet", () => {
    const { container } = render(<AssistantMarkdown content="[doc](/documents/42)" />);
    const a = container.querySelector("a");
    expect(a?.getAttribute("href")).toBe("/documents/42");
    expect(a?.getAttribute("target")).toBeNull();
  });
});

describe("AssistantMarkdown — F13 accessibilité du wrapper", () => {
  it("expose une région scrollable atteignable au clavier", () => {
    render(<AssistantMarkdown content={VALID_TABLE} />);

    const region = screen.getByRole("region", { name: "Tableau de résultats" });
    expect(region).not.toBeNull();
    expect(region.getAttribute("tabindex")).toBe("0");
    expect(region.querySelector("table")).not.toBeNull();
  });
});

describe("AssistantMarkdown — F14 classes et tokens", () => {
  it("n'introduit aucune couleur hexadécimale dans le composant", () => {
    const source = readFileSync(COMPONENT_PATH, "utf-8");
    expect(source).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    expect(source).not.toContain("dangerouslySetInnerHTML");
    // rehype-raw ne doit jamais être importé (il est cité en commentaire, à dessein).
    expect(source).not.toMatch(/from\s+["']rehype-raw["']/);
  });

  it("la feuille CSS s'appuie sur les tokens RagATM", () => {
    const css = readFileSync(CSS_PATH, "utf-8");
    for (const token of [
      "var(--surface)",
      "var(--bg)",
      "var(--border)",
      "var(--border-strong)",
      "var(--ink)",
      "var(--ink-2)",
      "var(--hover)",
      "var(--accent)",
      "var(--accent-ink)",
      "var(--shadow-card)",
    ]) {
      expect(css).toContain(token);
    }
    // Aucune couleur en dur : tokens.css reste la source unique de vérité.
    expect(css).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    expect(css).toContain("overflow-x: auto");
    expect(css).toContain("border-radius: 12px");
  });

  it("le wrapper porte bien les classes du module CSS", () => {
    const { container } = render(<AssistantMarkdown content={VALID_TABLE} />);
    const region = screen.getByRole("region", { name: "Tableau de résultats" });

    expect(region.className).toBeTruthy();
    expect(container.querySelector("table")?.className).toBeTruthy();
    expect(container.querySelector("th")?.className).toBeTruthy();
  });
});
