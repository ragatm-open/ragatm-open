/**
 * Modales de quota de la console plateforme (superuser).
 * Vérifie : préremplissage des défauts à la création, envoi du quota en octets, et
 * confirmation explicite d'une réduction SOUS l'usage courant (jamais appliquée au
 * premier clic).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const getWorkspaceQuotaDefaults = vi.fn();
const setWorkspaceQuota = vi.fn();
const createWorkspace = vi.fn();
const listPlatformUsers = vi.fn();

vi.mock("@/lib/platformAdmin", () => ({
  getWorkspaceQuotaDefaults: (...a: any[]) => getWorkspaceQuotaDefaults(...a),
  setWorkspaceQuota: (...a: any[]) => setWorkspaceQuota(...a),
  createWorkspace: (...a: any[]) => createWorkspace(...a),
  listPlatformUsers: (...a: any[]) => listPlatformUsers(...a),
  humanPlatformError: (_e: unknown, fb: string) => fb,
}));

import { CreateWorkspaceModal, WorkspaceQuotaModal } from "@/app/platform-admin/_components/PlatformModals";

const DEFAULTS = {
  default_max_document_count: 1000,
  default_max_document_bytes: 10 * 1024 ** 3,
  hard_max_document_count: 100000,
  hard_max_document_bytes: 1024 ** 4,
};

beforeEach(() => {
  vi.clearAllMocks();
  getWorkspaceQuotaDefaults.mockResolvedValue(DEFAULTS);
  listPlatformUsers.mockResolvedValue({ items: [], total: 0, limit: 8, offset: 0 });
});

describe("CreateWorkspaceModal — section quota", () => {
  it("charge et préremplit les défauts de plateforme", async () => {
    render(<CreateWorkspaceModal onClose={vi.fn()} onDone={vi.fn()} />);
    // La section apparaît après le chargement des défauts.
    expect(await screen.findByText("Limites de stockage documentaire")).toBeTruthy();
    expect(screen.getByDisplayValue("1000")).toBeTruthy();
    expect(screen.getByText(/Défauts de plateforme/)).toBeTruthy();
  });
});

const USAGE_OVER = {
  max_document_count: 1000,
  used_document_count: 100,
  remaining_document_count: 900,
  max_document_bytes: 10 * 1024 ** 3,
  used_document_bytes: 5 * 1024 ** 3, // 5 Gio consommés
  remaining_document_bytes: 5 * 1024 ** 3,
  count_usage_percent: 10,
  size_usage_percent: 50,
  is_count_exceeded: false,
  is_size_exceeded: false,
  is_exceeded: false,
  can_upload: true,
  enforced: true,
};

const WORKSPACE: any = {
  id: 42,
  name: "HR",
  quota: USAGE_OVER,
  owners: [],
  is_active: true,
};

describe("WorkspaceQuotaModal — réduction sous usage", () => {
  it("exige une confirmation avant d'appliquer une réduction sous l'usage", async () => {
    setWorkspaceQuota.mockResolvedValue({ code: "workspace_quota_updated", quota: USAGE_OVER });
    render(<WorkspaceQuotaModal workspace={WORKSPACE} onClose={vi.fn()} onDone={vi.fn()} />);

    // Attendre le chargement des défauts (section éditable) : le champ taille
    // apparaît prérempli à la limite courante (10 Gio).
    const sizeInput = await screen.findByDisplayValue("10");

    // Réduire à 1 Gio, sous les 5 Gio consommés.
    fireEvent.change(sizeInput, { target: { value: "1" } });

    // L'avertissement de réduction apparaît.
    expect(await screen.findByText(/sous l'usage actuel/)).toBeTruthy();

    // Premier clic : NE PAS appliquer (revue de la réduction).
    fireEvent.click(screen.getByText("Vérifier la réduction"));
    expect(setWorkspaceQuota).not.toHaveBeenCalled();

    // Second clic : appliquer.
    fireEvent.click(screen.getByText("Enregistrer quand même"));
    await waitFor(() => expect(setWorkspaceQuota).toHaveBeenCalledTimes(1));
    const [, body] = setWorkspaceQuota.mock.calls[0];
    expect(body.max_document_bytes).toBe(1024 ** 3);
  });
});
