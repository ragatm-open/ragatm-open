/**
 * Sélecteur d'espace : recherche, clavier, favoris, récents.
 * Rendu sur le VRAI WorkspaceProvider (seule l'API est doublée).
 */
import React from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiFetch = vi.fn();
vi.mock("@/lib/api", () => ({
  apiFetch: (...a: any[]) => apiFetch(...a),
  bootstrap: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn(), push: vi.fn() }) }));

import { SpaceSwitcher, WorkspaceProvider, useWorkspace } from "@/components/shell";

const ME = { id: "u1", email: "u@x.com", username: "u" };
const SPACES = [
  { id: "A", name: "Alpha" },
  { id: "B", name: "Dépenses publiques" },
  { id: "C", name: "Answer-Marche" },
];

function routeApi(url: string) {
  if (url.startsWith("/api/v1/auth/me/")) return Promise.resolve(ME);
  if (url === "/api/v1/workspaces/") return Promise.resolve([{ id: 1, name: "W1", role: "owner" }]);
  if (url.startsWith("/api/v1/rag-spaces/?workspace_id=")) return Promise.resolve(SPACES);
  if (url.includes("/stats/")) return Promise.resolve(null);
  return Promise.resolve({});
}

function Sel() {
  const { ragSpaceId } = useWorkspace();
  return <div data-testid="sel">{ragSpaceId}</div>;
}

async function mountOpen() {
  render(
    <WorkspaceProvider>
      <SpaceSwitcher />
      <Sel />
    </WorkspaceProvider>
  );
  await waitFor(() => expect(screen.getByTestId("sel")).toHaveTextContent("A"));
  fireEvent.click(screen.getByRole("button", { expanded: false, name: /Alpha/ }));
  const input = await screen.findByRole("combobox", { name: "Rechercher un espace" });
  return input;
}

const options = () => screen.getAllByRole("option");

describe("SpaceSwitcher — recherche", () => {
  beforeEach(() => {
    localStorage.clear();
    apiFetch.mockReset();
    apiFetch.mockImplementation(routeApi);
  });

  it("ouvre avec le champ focalisé et la section « Tous les espaces »", async () => {
    const input = await mountOpen();
    expect(input).toHaveFocus();
    expect(screen.getByRole("group", { name: "Tous les espaces" })).toBeInTheDocument();
    expect(screen.getByText("Favoris enregistrés sur cet appareil.")).toBeInTheDocument();
  });

  it("filtre sans accents ni casse et n'affiche que les résultats", async () => {
    const input = await mountOpen();
    fireEvent.change(input, { target: { value: "DEPEN" } });
    expect(screen.getByRole("group", { name: "Résultats" })).toBeInTheDocument();
    expect(options()).toHaveLength(1);
    expect(options()[0]).toHaveTextContent("Dépenses publiques");
    // Aucun appel réseau pour filtrer les espaces.
    expect(apiFetch.mock.calls.some(([u]) => String(u).includes("search"))).toBe(false);
  });

  it("↓ puis Entrée ouvre l'espace actif ; aria-activedescendant suit", async () => {
    const input = await mountOpen();
    fireEvent.change(input, { target: { value: "a" } });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    const first = options()[0];
    expect(input).toHaveAttribute("aria-activedescendant", first.id);
    expect(first).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(input).toHaveAttribute("aria-activedescendant", options()[1].id);
    fireEvent.keyDown(input, { key: "ArrowUp" });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(screen.queryByRole("combobox")).not.toBeInTheDocument());
    // « a » : Alpha (préfixe) devant Answer-Marche (préfixe) par nom.
    expect(screen.getByTestId("sel")).toHaveTextContent("A");
  });

  it("Entrée sans flèche ouvre le premier résultat", async () => {
    const input = await mountOpen();
    fireEvent.change(input, { target: { value: "marche" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(screen.getByTestId("sel")).toHaveTextContent("C"));
  });

  it("Échap ferme le sélecteur", async () => {
    const input = await mountOpen();
    fireEvent.keyDown(input, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("combobox")).not.toBeInTheDocument());
  });

  it("état vide explicite", async () => {
    const input = await mountOpen();
    fireEvent.change(input, { target: { value: "zzz" } });
    expect(screen.getByRole("status")).toHaveTextContent("Aucun espace ne correspond à « zzz »");
  });
});

describe("SpaceSwitcher — favoris et récents", () => {
  beforeEach(() => {
    localStorage.clear();
    apiFetch.mockReset();
    apiFetch.mockImplementation(routeApi);
  });

  it("★ ajoute puis retire un favori, stocké par utilisateur", async () => {
    await mountOpen();
    const all = screen.getByRole("group", { name: "Tous les espaces" });
    fireEvent.click(within(all).getByRole("button", { name: "Ajouter « Dépenses publiques » aux favoris" }));
    expect(JSON.parse(localStorage.getItem("ragatm_nav_favorites:u1")!)).toEqual(["B"]);
    const fav = screen.getByRole("group", { name: "Favoris" });
    expect(within(fav).getByText("Dépenses publiques")).toBeInTheDocument();
    fireEvent.click(within(fav).getByRole("button", { name: "Retirer « Dépenses publiques » des favoris" }));
    expect(JSON.parse(localStorage.getItem("ragatm_nav_favorites:u1")!)).toEqual([]);
    expect(screen.queryByRole("group", { name: "Favoris" })).not.toBeInTheDocument();
  });

  it("un favori d'un espace absent de la liste n'est pas affiché", async () => {
    localStorage.setItem("ragatm_nav_favorites:u1", JSON.stringify(["GONE", "C"]));
    await mountOpen();
    const fav = screen.getByRole("group", { name: "Favoris" });
    expect(within(fav).getAllByRole("option")).toHaveLength(1);
    expect(within(fav).getByText("Answer-Marche")).toBeInTheDocument();
  });

  it("les favoris d'un autre utilisateur ne s'affichent pas", async () => {
    localStorage.setItem("ragatm_nav_favorites:u2", JSON.stringify(["B"]));
    await mountOpen();
    expect(screen.queryByRole("group", { name: "Favoris" })).not.toBeInTheDocument();
  });

  it("l'espace ouvert devient récent", async () => {
    const input = await mountOpen();
    fireEvent.change(input, { target: { value: "depenses" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(screen.getByTestId("sel")).toHaveTextContent("B"));
    expect(JSON.parse(localStorage.getItem("ragatm_nav_recents:u1")!)).toEqual(["B", "A"]);
  });
});
