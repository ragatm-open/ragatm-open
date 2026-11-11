/**
 * Frontière de rendu d'AssistantMarkdown.
 *
 * Le rendu Markdown re-parse tout le texte à chaque appel. Un rerendu du parent
 * avec un contenu identique ne doit donc pas le relancer ; un contenu différent
 * (jeton de streaming) doit, lui, être rendu.
 *
 * `react-markdown` est remplacé par une doublure qui compte ses appels : le test
 * observe le travail effectivement relancé, sans dépendre des internes de React.
 */
import React from "react";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const parses: { content: string; plugins: unknown }[] = [];

vi.mock("react-markdown", () => ({
  default: ({ children, remarkPlugins }: any) => {
    parses.push({ content: String(children), plugins: remarkPlugins });
    return <div data-testid="md">{children}</div>;
  },
  defaultUrlTransform: (u: string) => u,
}));

import { AssistantMarkdown } from "@/app/chat/_components/AssistantMarkdown";

function Parent({ content, tick }: { content: string; tick: number }) {
  return (
    <div data-tick={tick}>
      <AssistantMarkdown content={content} />
    </div>
  );
}

describe("AssistantMarkdown — mémoïsation", () => {
  beforeEach(() => {
    parses.length = 0;
  });

  it("un rerendu du parent à contenu identique ne relance pas le rendu Markdown", () => {
    const view = render(<Parent content="Réponse **stable** [1]." tick={0} />);
    expect(parses).toHaveLength(1);
    for (let tick = 1; tick <= 5; tick++) view.rerender(<Parent content="Réponse **stable** [1]." tick={tick} />);
    expect(parses).toHaveLength(1);
    expect(screen.getByTestId("md").textContent).toBe("Réponse **stable** [1].");
  });

  it("un contenu modifié (streaming) est rendu à chaque changement", () => {
    const view = render(<Parent content="Rép" tick={0} />);
    view.rerender(<Parent content="Répon" tick={1} />);
    view.rerender(<Parent content="Réponse." tick={2} />);
    expect(parses.map((p) => p.content)).toEqual(["Rép", "Répon", "Réponse."]);
    expect(screen.getByTestId("md").textContent).toBe("Réponse.");
  });

  it("la liste de plugins est la même référence d'un rendu à l'autre", () => {
    const view = render(<Parent content="a" tick={0} />);
    view.rerender(<Parent content="b" tick={1} />);
    expect(parses).toHaveLength(2);
    expect(parses[0].plugins).toBe(parses[1].plugins);
  });
});
