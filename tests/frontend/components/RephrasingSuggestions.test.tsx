import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { RephrasingSuggestions } from "@/app/chat/_components/RephrasingSuggestions";

const SUGGESTIONS = [
  "Quels risques de retard sont mentionnés dans les documents ?",
  "Le contrat précise-t-il des pénalités en cas de retard ?",
];

describe("RephrasingSuggestions", () => {
  it("affiche une suggestion par bouton sur une non-réponse", () => {
    render(<RephrasingSuggestions suggestions={SUGGESTIONS} onPick={vi.fn()} />);
    const group = screen.getByRole("group", { name: "Suggestions de reformulation" });
    expect(group).toBeInTheDocument();
    for (const s of SUGGESTIONS) {
      expect(screen.getByRole("button", { name: `Reformuler : ${s}` })).toBeInTheDocument();
    }
  });

  it("est masqué quand la liste est vide (aucune suggestion sur réponse valide)", () => {
    const { container } = render(<RephrasingSuggestions suggestions={[]} onPick={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("group")).toBeNull();
  });

  it("ignore les chaînes vides", () => {
    render(<RephrasingSuggestions suggestions={["", "   ", "Quelle échéance ?"]} onPick={vi.fn()} />);
    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(1);
    expect(buttons[0]).toHaveTextContent("Quelle échéance ?");
  });

  it("au clic, remplit le composer via onPick (pas d'envoi automatique)", () => {
    const onPick = vi.fn();
    render(<RephrasingSuggestions suggestions={SUGGESTIONS} onPick={onPick} />);
    fireEvent.click(screen.getByRole("button", { name: `Reformuler : ${SUGGESTIONS[0]}` }));
    expect(onPick).toHaveBeenCalledTimes(1);
    expect(onPick).toHaveBeenCalledWith(SUGGESTIONS[0]);
  });

  it("n'appelle pas onPick au rendu (aucune soumission implicite)", () => {
    const onPick = vi.fn();
    render(<RephrasingSuggestions suggestions={SUGGESTIONS} onPick={onPick} />);
    expect(onPick).not.toHaveBeenCalled();
  });

  it("désactive les boutons quand disabled (pas de double requête pendant un envoi)", () => {
    const onPick = vi.fn();
    render(<RephrasingSuggestions suggestions={SUGGESTIONS} onPick={onPick} disabled />);
    const btn = screen.getByRole("button", { name: `Reformuler : ${SUGGESTIONS[0]}` }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    fireEvent.click(btn);
    expect(onPick).not.toHaveBeenCalled();
  });

  it("les boutons sont focusables au clavier (navigation accessible)", () => {
    render(<RephrasingSuggestions suggestions={SUGGESTIONS} onPick={vi.fn()} />);
    const btn = screen.getByRole("button", { name: `Reformuler : ${SUGGESTIONS[0]}` });
    btn.focus();
    expect(btn).toHaveFocus();
  });
});
