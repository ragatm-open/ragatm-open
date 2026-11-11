/**
 * Suppression définitive d'un RagSpace — UI.
 *
 * La purge est irréversible : l'interface ne doit ni mentir sur ce qu'elle fait,
 * ni retirer l'espace de la liste si le backend a échoué, ni permettre un double clic.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const apiFetch = vi.fn();
vi.mock("@/lib/api", () => ({ apiFetch: (...a: any[]) => apiFetch(...a) }));

// Contexte workspace : la tab l'utilise pour tenir le sélecteur global à jour.
const upsertRagSpace = vi.fn();
const refreshRagSpaces = vi.fn();
const setRagSpaceId = vi.fn();
// accessForRagSpace : rôle effectif inconnu par défaut => bouton « Accès » masqué
// (posture restrictive) ; sans incidence sur le parcours de suppression testé ici.
vi.mock("@/components/shell/WorkspaceContext", () => ({
  useWorkspace: () => ({
    upsertRagSpace,
    refreshRagSpaces,
    setRagSpaceId,
    accessForRagSpace: () => ({ permissionLevel: null, spaceRole: null, isOwnerImplicit: false }),
  }),
}));

import { SpacesTab } from "@/app/workspace-admin/_components/SpacesTab";

const SPACE = {
  id: "space-1",
  name: "RH",
  llm_provider: "ollama",
  llm_model: "",
  embedding_provider: "ollama",
  embedding_model: "nomic-embed-text",
} as any;

function renderTab(overrides: Partial<Record<string, any>> = {}) {
  const onChanged = overrides.onChanged ?? vi.fn().mockResolvedValue(undefined);
  const setErr = overrides.setErr ?? vi.fn();
  render(
    <SpacesTab
      workspaceId={1}
      ragSpaces={[SPACE]}
      loading={false}
      onChanged={onChanged}
      setErr={setErr}
    />,
  );
  return { onChanged, setErr };
}

function openModal() {
  fireEvent.click(screen.getAllByRole("button", { name: /supprimer/i })[0]);
}

/**
 * Au montage, SpacesTab interroge le job de réindexation actif de chaque espace.
 * Cet appel passe par le même apiFetch que la suppression : router les réponses par
 * URL (et non par ordre d'appel via `*Once`) est donc la seule façon fiable de piloter
 * le seul DELETE. Sans cela, l'appel du montage consomme le mock destiné au DELETE.
 */
function mockApi(onDelete: () => Promise<unknown> = async () => undefined) {
  apiFetch.mockImplementation((url: string, init?: { method?: string }) => {
    if (String(url).includes("/active-reindex-job/")) {
      return Promise.resolve({ active: false, job: null });
    }
    if (init?.method === "DELETE") return onDelete();
    return Promise.resolve(undefined);
  });
}

/** Seuls les DELETE nous intéressent : le montage émet ses propres appels. */
function deleteCalls() {
  return apiFetch.mock.calls.filter(([, init]: any[]) => init?.method === "DELETE");
}

beforeEach(() => {
  vi.clearAllMocks();
  mockApi();
});

describe("SpacesTab — suppression définitive", () => {
  it("la modale annonce une purge irréversible incluant les fichiers", () => {
    renderTab();
    openModal();

    // « Supprimer définitivement » apparaît au titre ET sur le bouton : les deux
    // sont voulus, on vérifie donc la présence sans exiger l'unicité.
    expect(screen.getAllByText(/Supprimer définitivement/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/ne peut pas être annulée/i)).toBeInTheDocument();
    // La promesse doit couvrir documents, index ET fichiers — c'est ce que fait le backend.
    const warn = screen.getByText(/seront supprimés du serveur/i);
    expect(warn.textContent).toMatch(/documents/i);
    expect(warn.textContent).toMatch(/index/i);
    expect(warn.textContent).toMatch(/fichiers/i);
    expect(warn.textContent).toMatch(/irréversible/i);
  });

  it("appelle DELETE sur le bon endpoint et retire l'espace après succès", async () => {
    mockApi(async () => undefined); // 204
    const { onChanged } = renderTab();
    openModal();
    fireEvent.click(screen.getByRole("button", { name: /^Supprimer définitivement$/i }));

    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith("/api/v1/rag-spaces/space-1/", { method: "DELETE" }),
    );
    // La liste n'est rafraîchie qu'APRÈS confirmation backend.
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("affiche « Suppression en cours… » et bloque le double clic pendant la requête", async () => {
    let resolve!: () => void;
    mockApi(() => new Promise<void>((r) => (resolve = r)));
    renderTab();
    openModal();

    const btn = screen.getByRole("button", { name: /^Supprimer définitivement$/i });
    fireEvent.click(btn);

    await waitFor(() => expect(screen.getByText(/Suppression en cours/i)).toBeInTheDocument());

    // Deuxième clic pendant la requête : ne doit PAS relancer une purge.
    fireEvent.click(screen.getByRole("button", { name: /Suppression en cours/i }));
    expect(deleteCalls()).toHaveLength(1);

    resolve();
  });

  it("en cas d'échec backend, l'espace RESTE et un message clair est affiché", async () => {
    mockApi(async () => {
      throw new Error("La suppression définitive de l'espace a échoué.");
    });
    const { onChanged, setErr } = renderTab();
    openModal();
    fireEvent.click(screen.getByRole("button", { name: /^Supprimer définitivement$/i }));

    await waitFor(() => expect(setErr).toHaveBeenCalled());
    // Ne JAMAIS prétendre au succès : pas de rafraîchissement de liste.
    expect(onChanged).not.toHaveBeenCalled();
    // La modale reste ouverte : l'utilisateur peut réessayer.
    expect(screen.getByText(/ne peut pas être annulée/i)).toBeInTheDocument();
    const msg = String(setErr.mock.calls[0][0]);
    expect(msg.length).toBeGreaterThan(0);
  });

  it("réactive le bouton après un échec (nouvelle tentative possible)", async () => {
    mockApi(async () => {
      throw new Error("boom");
    });
    renderTab();
    openModal();
    fireEvent.click(screen.getByRole("button", { name: /^Supprimer définitivement$/i }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^Supprimer définitivement$/i })).not.toBeDisabled(),
    );
  });
});
