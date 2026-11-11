/**
 * Import de dossier avec sélection des formats — FLUX RÉEL de la page Documents.
 *
 * Ces tests ne vérifient PAS que la modale est visible : ils vérifient ce qui part
 * sur le réseau. Un format décoché ne doit produire AUCUNE requête d'upload, et
 * l'import doit rester strictement additif (aucun DELETE déclenché).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const apiFetch = vi.fn();
const apiUploadForm = vi.fn();
const deleteDocument = vi.fn();
vi.mock("@/lib/api", () => ({
  apiFetch: (...a: any[]) => apiFetch(...a),
  apiUploadForm: (...a: any[]) => apiUploadForm(...a),
  apiFetchBlobUrl: vi.fn(),
  deleteDocument: (...a: any[]) => deleteDocument(...a),
  getCapabilities: vi.fn().mockResolvedValue({ embeddings: { provider: "ollama" } }),
  summarizeDocumentAwait: vi.fn(),
  summaryErrorKey: vi.fn(),
}));

const refreshStats = vi.fn();
vi.mock("@/components/shell", () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Topbar: () => null,
  useWorkspace: () => ({
    ragSpace: { id: "SP1", name: "RH", embedding_provider: "ollama" },
    ragSpaceId: "SP1",
    ragSpaceAccess: { permissionLevel: "write", spaceRole: null, isOwnerImplicit: false },
    canWriteCurrent: true,
    canManageCurrent: false,
    refreshStats,
  }),
}));

import {
  UPLOAD_MIN_INTERVAL_MS,
  __resetUploadScheduler,
} from "@/app/documents/_lib/uploadScheduler";
import DocumentsPage from "@/app/documents/page";

function fakeFile(relPath: string, size = 1000): File {
  const name = relPath.split("/").pop() || relPath;
  const f = new File([new Uint8Array(size)], name, { type: "" });
  Object.defineProperty(f, "webkitRelativePath", { value: relPath, configurable: true });
  return f;
}

/** Dossier mixte, avec sous-dossier et format non supporté. */
const FOLDER = [
  fakeFile("Dossier/rapport.pdf"),
  fakeFile("Dossier/note.docx"),
  fakeFile("Dossier/presentation.pptx"),
  fakeFile("Dossier/donnees.xlsx"),
  fakeFile("Dossier/sous-dossier/annexe.PDF"),
  fakeFile("Dossier/sous-dossier/image.png"),
  fakeFile("Dossier/sous-dossier/binaire.exe"),
];

/** Sélectionne un dossier dans l'input natif (webkitdirectory). */
function selectFolder(container: HTMLElement, files: File[]) {
  const input = container.querySelector("input[webkitdirectory]") as HTMLInputElement;
  expect(input).toBeTruthy();
  Object.defineProperty(input, "files", { value: files, configurable: true });
  fireEvent.change(input);
}

/** Noms des fichiers réellement envoyés au backend. */
function uploadedNames(): string[] {
  return apiUploadForm.mock.calls.map((c) => (c[1] as FormData).get("file") as File).map((f) => f.name);
}

function decocher(label: RegExp) {
  fireEvent.click(screen.getByRole("checkbox", { name: label }));
}

async function confirmer() {
  fireEvent.click(screen.getByRole("button", { name: /Importer et indexer/i }));
  await waitFor(() => expect(apiUploadForm).toHaveBeenCalled());
}

/**
 * Attend que N envois soient RÉELLEMENT partis.
 *
 * Depuis l'ordonnanceur de débit, les départs sont espacés d'au moins
 * `UPLOAD_MIN_INTERVAL_MS` : six fichiers prennent donc plusieurs secondes, là où ils
 * partaient auparavant en rafale. C'est le comportement voulu — c'est la rafale qui
 * était le défaut. Le délai d'attente est allongé en conséquence, jamais la cadence
 * réduite pour faire plaisir aux tests.
 */
async function attendreEnvois(n: number) {
  await waitFor(() => expect(apiUploadForm).toHaveBeenCalledTimes(n), {
    timeout: (n + 2) * UPLOAD_MIN_INTERVAL_MS,
  });
}

beforeEach(() => {
  // La cadence d'envoi est PARTAGÉE au niveau du module (un onglet = une
  // cadence). Sans remise à zéro, le créneau réservé par un test repousse le
  // premier envoi du suivant et le fait expirer.
  __resetUploadScheduler();
  vi.clearAllMocks();
  apiFetch.mockResolvedValue({ items: [] });
  apiUploadForm.mockResolvedValue({ ok: true });
});

describe("Documents — import de dossier : la sélection des formats filtre le réseau", () => {
  it("aucun upload n'est déclenché par la seule sélection du dossier", async () => {
    const { container } = render(<DocumentsPage />);
    selectFolder(container, FOLDER);
    // La modale s'ouvre...
    expect(await screen.findByText("Dossier : Dossier")).toBeInTheDocument();
    // ...et RIEN n'est parti.
    expect(apiUploadForm).not.toHaveBeenCalled();
  });

  it("tous les formats cochés par défaut => tous les fichiers supportés sont envoyés", async () => {
    const { container } = render(<DocumentsPage />);
    selectFolder(container, FOLDER);
    await screen.findByText("Dossier : Dossier");
    for (const cb of screen.getAllByRole("checkbox")) expect(cb).toBeChecked();

    await confirmer();
    await attendreEnvois(6);
    expect(uploadedNames().sort()).toEqual(
      ["rapport.pdf", "note.docx", "presentation.pptx", "donnees.xlsx", "annexe.PDF", "image.png"].sort()
    );
    // Le fichier non supporté n'est jamais envoyé.
    expect(uploadedNames()).not.toContain("binaire.exe");
  });

  it("PDF + PowerPoint seuls : exactement 3 requêtes, aucune pour les formats décochés", async () => {
    const { container } = render(<DocumentsPage />);
    selectFolder(container, FOLDER);
    await screen.findByText("Dossier : Dossier");

    decocher(/Word/i);
    decocher(/Excel/i);
    decocher(/Images/i);
    await confirmer();

    await attendreEnvois(3);
    expect(uploadedNames().sort()).toEqual(["annexe.PDF", "presentation.pptx", "rapport.pdf"]);
    for (const exclu of ["note.docx", "donnees.xlsx", "image.png", "binaire.exe"]) {
      expect(uploadedNames()).not.toContain(exclu);
    }
  });

  it("Images uniquement", async () => {
    const { container } = render(<DocumentsPage />);
    selectFolder(container, FOLDER);
    await screen.findByText("Dossier : Dossier");
    decocher(/PDF/i);
    decocher(/Word/i);
    decocher(/PowerPoint/i);
    decocher(/Excel/i);
    await confirmer();
    await attendreEnvois(1);
    expect(uploadedNames()).toEqual(["image.png"]);
  });

  it("aucun format sélectionné => aucune requête, import bloqué", async () => {
    const { container } = render(<DocumentsPage />);
    selectFolder(container, FOLDER);
    await screen.findByText("Dossier : Dossier");
    for (const label of [/PDF/i, /Word/i, /PowerPoint/i, /Excel/i, /Images/i]) decocher(label);

    expect(screen.getByText(/Sélectionnez au moins un format/i)).toBeInTheDocument();
    const btn = screen.getByRole("button", { name: /Importer et indexer/i });
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(apiUploadForm).not.toHaveBeenCalled();
  });

  it("Annuler ne déclenche aucun import et referme la modale", async () => {
    const { container } = render(<DocumentsPage />);
    selectFolder(container, FOLDER);
    await screen.findByText("Dossier : Dossier");
    fireEvent.click(screen.getByRole("button", { name: /Annuler/i }));
    await waitFor(() => expect(screen.queryByText("Dossier : Dossier")).not.toBeInTheDocument());
    expect(apiUploadForm).not.toHaveBeenCalled();
  });

  it("les chemins relatifs et le dossier racine sont transmis intacts", async () => {
    const { container } = render(<DocumentsPage />);
    selectFolder(container, FOLDER);
    await screen.findByText("Dossier : Dossier");
    decocher(/Word/i);
    decocher(/Excel/i);
    decocher(/Images/i);
    decocher(/PowerPoint/i);
    await confirmer();
    await attendreEnvois(2);

    const forms = apiUploadForm.mock.calls.map((c) => c[1] as FormData);
    const byName = new Map(forms.map((f) => [(f.get("file") as File).name, f]));
    expect(byName.get("rapport.pdf")?.get("relative_path")).toBe("rapport.pdf");
    expect(byName.get("annexe.PDF")?.get("relative_path")).toBe("sous-dossier/annexe.PDF");
    for (const f of forms) {
      expect(f.get("source_folder")).toBe("Dossier");
      expect(f.get("rag_space_id")).toBe("SP1");
    }
  });

  it("le payload ne porte AUCUNE intention destructive (import strictement additif)", async () => {
    const { container } = render(<DocumentsPage />);
    selectFolder(container, FOLDER);
    await screen.findByText("Dossier : Dossier");
    await confirmer();
    await attendreEnvois(6);

    for (const call of apiUploadForm.mock.calls) {
      const url = call[0] as string;
      expect(url).toBe("/api/v1/documents/upload/?async=true");
      const keys = [...(call[1] as FormData).keys()];
      expect(keys.sort()).toEqual(["file", "rag_space_id", "relative_path", "source_folder"].sort());
      for (const interdit of [
        "replace",
        "overwrite",
        "sync",
        "mirror",
        "delete_missing",
        "clear_existing",
        "reset",
        "purge",
        "replace_all",
        "mode",
      ]) {
        expect(keys).not.toContain(interdit);
      }
    }
    // Aucune suppression déclenchée par un import de dossier.
    expect(deleteDocument).not.toHaveBeenCalled();
  });

  it("une nouvelle sélection RÉINITIALISE la présélection (aucun report)", async () => {
    const { container } = render(<DocumentsPage />);
    selectFolder(container, FOLDER);
    await screen.findByText("Dossier : Dossier");
    decocher(/PDF/i); // on décoche...
    expect(screen.getByRole("checkbox", { name: /PDF/i })).not.toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: /Annuler/i }));
    await waitFor(() => expect(screen.queryByText("Dossier : Dossier")).not.toBeInTheDocument());

    // ...nouvelle sélection : tout est recoché, et la liste des formats est
    // recalculée pour CE dossier (pas de résidu de la sélection précédente).
    selectFolder(container, [fakeFile("Autre/x.pdf"), fakeFile("Autre/y.docx")]);
    await screen.findByText("Dossier : Autre");
    for (const cb of screen.getAllByRole("checkbox")) expect(cb).toBeChecked();
    expect(screen.queryByRole("checkbox", { name: /Images/i })).not.toBeInTheDocument();

    await confirmer();
    await attendreEnvois(2);
    expect(uploadedNames().sort()).toEqual(["x.pdf", "y.docx"]);
  });

  it("un échec partiel n'empêche pas les autres fichiers et ne supprime rien", async () => {
    apiUploadForm.mockImplementation((_url: string, form: FormData) => {
      const name = (form.get("file") as File).name;
      return name === "note.docx" ? Promise.reject(new Error("upload_failed")) : Promise.resolve({ ok: true });
    });
    const { container } = render(<DocumentsPage />);
    selectFolder(container, FOLDER);
    await screen.findByText("Dossier : Dossier");
    await confirmer();

    await attendreEnvois(6);
    // Les 5 autres sont bien partis malgré l'échec du 6e.
    expect(uploadedNames()).toContain("rapport.pdf");
    expect(uploadedNames()).toContain("presentation.pptx");
    expect(deleteDocument).not.toHaveBeenCalled();
  });
});
