/**
 * Ce que l'UTILISATEUR voit quand le serveur ralentit les envois.
 *
 * L'incident du 23/08/2026 n'était pas seulement technique : l'interface affichait
 * « Échec » à 135 fichiers pour une saturation de vingt et une secondes. Ces tests
 * vérifient qu'un 429 produit désormais une ATTENTE visible, que le fichier finit
 * par passer, et que le ré-essai manuel ne double pas le ré-essai automatique.
 *
 * La cadence est neutralisée ici (`__resetUploadScheduler(0)`) : le sujet est le
 * traitement du 429, pas le débit — celui-ci a sa propre suite.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const apiFetch = vi.fn();
const apiUploadForm = vi.fn();
vi.mock("@/lib/api", () => ({
  apiFetch: (...a: any[]) => apiFetch(...a),
  apiUploadForm: (...a: any[]) => apiUploadForm(...a),
  apiFetchBlobUrl: vi.fn(),
  deleteDocument: vi.fn(),
  getCapabilities: vi.fn().mockResolvedValue({ embeddings: { provider: "ollama" } }),
  summarizeDocumentAwait: vi.fn(),
  summaryErrorKey: vi.fn(),
}));

vi.mock("@/components/shell", () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Topbar: () => null,
  useWorkspace: () => ({
    ragSpace: { id: "SP1", name: "RH", embedding_provider: "ollama" },
    ragSpaceId: "SP1",
    ragSpaceAccess: { permissionLevel: "write", spaceRole: null, isOwnerImplicit: false },
    canWriteCurrent: true,
    canManageCurrent: false,
    refreshStats: vi.fn(),
  }),
}));

import { ApiError } from "@/lib/apiError";
import { __resetUploadScheduler } from "@/app/documents/_lib/uploadScheduler";
import DocumentsPage from "@/app/documents/page";

/** 429 tel que DRF le produit : `Retry-After` déjà converti en ms par la couche HTTP. */
function throttled(retryAfterMs: number | null) {
  return new ApiError(
    429,
    "Too Many Requests",
    { detail: "Requête ralentie. Expected available in 21 seconds." },
    "API 429 Too Many Requests: Requête ralentie.",
    retryAfterMs,
  );
}

function fakeFile(name: string): File {
  return new File([new Uint8Array(10)], name, { type: "application/pdf" });
}

function deposer(container: HTMLElement, files: File[]) {
  const input = container.querySelector('input[type="file"]:not([webkitdirectory])') as HTMLInputElement;
  expect(input).toBeTruthy();
  Object.defineProperty(input, "files", { value: files, configurable: true });
  fireEvent.change(input);
}

beforeEach(() => {
  vi.clearAllMocks();
  __resetUploadScheduler(0);
  apiFetch.mockResolvedValue({ items: [] });
});

describe("Documents — un 429 est une attente, pas un échec", () => {
  it("affiche « réessai dans N s » plutôt qu'une erreur, puis passe", async () => {
    // Premier envoi refusé, second accepté.
    apiUploadForm.mockRejectedValueOnce(throttled(1_000)).mockResolvedValue({ ok: true });

    const { container } = render(<DocumentsPage />);
    deposer(container, [fakeFile("rapport.pdf")]);

    // L'utilisateur voit une ATTENTE. Le code HTTP appartient au journal, pas à lui.
    await waitFor(() => expect(screen.getByText(/réessai dans/i)).toBeInTheDocument(), { timeout: 4_000 });
    expect(screen.queryByText(/^Échec$/)).not.toBeInTheDocument();

    // …puis le fichier passe, sans aucune action de sa part. En fin de lot les
    // réussis QUITTENT la zone d'envoi (ils sont désormais dans la liste des
    // documents) : on vérifie donc le second envoi et l'absence d'échec, pas un
    // libellé de succès qui n'a pas vocation à rester à l'écran.
    await waitFor(() => expect(apiUploadForm).toHaveBeenCalledTimes(2), { timeout: 6_000 });
    await waitFor(
      () => expect(screen.queryByRole("button", { name: /Réessayer les erreurs/i })).not.toBeInTheDocument(),
      { timeout: 4_000 },
    );
    expect(screen.queryByText(/limité le débit/i)).not.toBeInTheDocument();
  });

  it("pendant l'attente, « Réessayer les erreurs » n'est pas proposé", async () => {
    // Le ré-essai manuel doublerait le ré-essai automatique : deux envois du même
    // fichier, dont un que personne n'a demandé.
    apiUploadForm.mockRejectedValueOnce(throttled(3_000)).mockResolvedValue({ ok: true });

    const { container } = render(<DocumentsPage />);
    deposer(container, [fakeFile("rapport.pdf")]);

    await waitFor(() => expect(screen.getByText(/réessai dans/i)).toBeInTheDocument(), { timeout: 4_000 });
    expect(screen.queryByRole("button", { name: /Réessayer les erreurs/i })).not.toBeInTheDocument();
  });

  it("un quota durablement saturé finit en échec, avec un message qui n'accuse pas le fichier", async () => {
    apiUploadForm.mockRejectedValue(throttled(1_000));

    const { container } = render(<DocumentsPage />);
    deposer(container, [fakeFile("rapport.pdf")]);

    await waitFor(() => expect(screen.getByText(/limité le débit/i)).toBeInTheDocument(), {
      timeout: 15_000,
    });
    // Quatre tentatives, pas davantage : le ré-essai est borné.
    expect(apiUploadForm).toHaveBeenCalledTimes(4);
  });

  it("une erreur terminale reste une erreur, sans ré-essai", async () => {
    apiUploadForm.mockRejectedValue(
      new ApiError(400, "Bad Request", { detail: "format" }, "API 400 Bad Request: format"),
    );

    const { container } = render(<DocumentsPage />);
    deposer(container, [fakeFile("rapport.pdf")]);

    await waitFor(() => expect(apiUploadForm).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByRole("button", { name: /Réessayer les erreurs/i })).toBeInTheDocument(), {
      timeout: 4_000,
    });
    // Aucun ré-essai automatique n'a eu lieu : un 400 ne se rejoue pas.
    expect(apiUploadForm).toHaveBeenCalledTimes(1);
  });

  it("annuler pendant l'attente n'émet plus aucune requête", async () => {
    apiUploadForm.mockRejectedValueOnce(throttled(60_000)).mockResolvedValue({ ok: true });

    const { container } = render(<DocumentsPage />);
    deposer(container, [fakeFile("a.pdf"), fakeFile("b.pdf"), fakeFile("c.pdf")]);

    await waitFor(() => expect(apiUploadForm).toHaveBeenCalledTimes(1));
    const bouton = await screen.findByRole("button", { name: /Annuler l'import/i });
    fireEvent.click(bouton);

    const apres = apiUploadForm.mock.calls.length;
    await new Promise((r) => setTimeout(r, 300));
    expect(apiUploadForm.mock.calls.length).toBe(apres);
  });
});
