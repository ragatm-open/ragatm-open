/**
 * Les deux boutons qui remplacent la saisie de « réponse rapide » / « analyse
 * complète ».
 *
 * Le composant ne connaît que des identifiants d'action stables : aucun test ne
 * s'appuie sur le Markdown affiché, et changer un libellé ne doit rien casser.
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { CorpusScopeChoice } from "@/app/chat/_components/CorpusScopeChoice";

describe("CorpusScopeChoice", () => {
  it("affiche les deux vrais boutons, dans un groupe nommé", () => {
    render(<CorpusScopeChoice onChoose={vi.fn()} globalAnalysisAvailable />);

    const group = screen.getByRole("group", { name: /Choisir le mode d’analyse/ });
    expect(within(group).getAllByRole("button")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Réponse rapide" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Analyse complète" })).toBeInTheDocument();
  });

  it("n'offre pas l'analyse complète quand le moteur ne sait pas la mener", () => {
    render(<CorpusScopeChoice onChoose={vi.fn()} globalAnalysisAvailable={false} />);

    expect(screen.getByRole("button", { name: "Réponse rapide" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Analyse complète" })).not.toBeInTheDocument();
  });

  it.each([
    ["Réponse rapide", "quick_response"],
    ["Analyse complète", "global_analysis"],
  ])("remonte l'identifiant d'action stable au clic sur %s", async (label, id) => {
    const onChoose = vi.fn();
    render(<CorpusScopeChoice onChoose={onChoose} globalAnalysisAvailable />);

    await userEvent.click(screen.getByRole("button", { name: label }));

    expect(onChoose).toHaveBeenCalledWith(id);
  });

  it("désactive LES DEUX boutons dès qu'un choix part : le double clic ne peut pas doubler", async () => {
    const onChoose = vi.fn();
    const { rerender } = render(
      <CorpusScopeChoice onChoose={onChoose} globalAnalysisAvailable />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Analyse complète" }));
    // Ce que fait la page : le choix passe « en vol ».
    rerender(
      <CorpusScopeChoice onChoose={onChoose} globalAnalysisAvailable pending="global_analysis" />,
    );

    const busy = screen.getByRole("button", { name: /Lancement/ });
    expect(busy).toBeDisabled();
    expect(busy).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("button", { name: "Réponse rapide" })).toBeDisabled();

    // Un second clic ne peut plus rien envoyer.
    await userEvent.click(busy);
    expect(onChoose).toHaveBeenCalledTimes(1);
  });

  it("reste utilisable au clavier", async () => {
    const onChoose = vi.fn();
    render(<CorpusScopeChoice onChoose={onChoose} globalAnalysisAvailable />);

    await userEvent.tab();
    expect(screen.getByRole("button", { name: "Réponse rapide" })).toHaveFocus();
    await userEvent.tab();
    expect(screen.getByRole("button", { name: "Analyse complète" })).toHaveFocus();

    await userEvent.keyboard("{Enter}");
    expect(onChoose).toHaveBeenCalledWith("global_analysis");
  });

  it("se replie sur mobile plutôt que de déborder", () => {
    render(<CorpusScopeChoice onChoose={vi.fn()} globalAnalysisAvailable />);

    // Un conteneur qui autorise le retour à la ligne : à 390 px les boutons
    // s'empilent au lieu de provoquer un défilement horizontal.
    expect(screen.getByTestId("corpus-scope-choice")).toHaveStyle({ flexWrap: "wrap" });
  });

  it("peut être désactivé de l'extérieur (envoi impossible)", () => {
    render(<CorpusScopeChoice onChoose={vi.fn()} globalAnalysisAvailable disabled />);

    for (const name of ["Réponse rapide", "Analyse complète"]) {
      expect(screen.getByRole("button", { name })).toBeDisabled();
    }
  });
});
