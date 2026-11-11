/**
 * Verrous temporaires Office — FLUX RÉEL de la page Documents.
 *
 * Ce fichier ne vérifie pas un affichage : il vérifie **ce qui part sur le réseau**.
 * Un filtre purement visuel laisserait le symptôme intact — 18 requêtes, 18 refus,
 * 18 lignes rouges. L'assertion centrale est donc le nombre exact d'appels à
 * `apiUploadForm`, et l'absence totale d'appel pour un `~$`.
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

import { __resetUploadScheduler } from "@/app/documents/_lib/uploadScheduler";
import DocumentsPage from "@/app/documents/page";

function fakeFile(relPath: string, size = 1000): File {
  const name = relPath.split("/").pop() || relPath;
  const f = new File([new Uint8Array(size)], name, { type: "" });
  Object.defineProperty(f, "webkitRelativePath", { value: relPath, configurable: true });
  return f;
}

/** Le scénario de la mission : chaque document a son verrou. */
const FOLDER = [
  fakeFile("D/rapport.pdf"),
  fakeFile("D/~$rapport.pdf"),
  fakeFile("D/note.docx"),
  fakeFile("D/~$note.docx"),
  fakeFile("D/presentation.pptx"),
  fakeFile("D/~$presentation.pptx"),
  fakeFile("D/budget.xlsx"),
  fakeFile("D/~$budget.xlsx"),
];

function selectFolder(container: HTMLElement, files: File[]) {
  const input = container.querySelector("input[webkitdirectory]") as HTMLInputElement;
  expect(input).toBeTruthy();
  Object.defineProperty(input, "files", { value: files, configurable: true });
  fireEvent.change(input);
}

function selectFiles(container: HTMLElement, files: File[]) {
  const input = container.querySelector(
    'input[type="file"]:not([webkitdirectory])'
  ) as HTMLInputElement;
  expect(input).toBeTruthy();
  Object.defineProperty(input, "files", { value: files, configurable: true });
  fireEvent.change(input);
}

function uploadedNames(): string[] {
  return apiUploadForm.mock.calls
    .map((c) => (c[1] as FormData).get("file") as File)
    .map((f) => f.name);
}

async function confirmer() {
  fireEvent.click(screen.getByRole("button", { name: /Importer et indexer/i }));
}

beforeEach(() => {
  // La cadence d'envoi est PARTAGÉE au niveau du module (un onglet = une
  // cadence). Sans remise à zéro, le créneau réservé par un test repousse le
  // premier envoi du suivant et le fait expirer.
  // Sujet de ce fichier : aucun verrou Office n'atteint le réseau. La cadence est
  // neutralisée ici — elle a sa propre suite, à bornes absolues.
  __resetUploadScheduler(0);
  vi.clearAllMocks();
  apiFetch.mockResolvedValue({ items: [] });
  apiUploadForm.mockResolvedValue({ ok: true });
});

describe("import de dossier — aucun verrou n'atteint le réseau", () => {
  it("4 documents envoyés, 0 appel pour les 4 verrous", async () => {
    const { container } = render(<DocumentsPage />);
    selectFolder(container, FOLDER);
    await screen.findByText("Dossier : D");
    await confirmer();

    await waitFor(() => expect(apiUploadForm).toHaveBeenCalledTimes(4));
    expect(uploadedNames().sort()).toEqual(
      ["budget.xlsx", "note.docx", "presentation.pptx", "rapport.pdf"].sort()
    );
    // L'assertion qui compte : aucun `~$` n'a construit de FormData.
    expect(uploadedNames().some((n) => n.startsWith("~$"))).toBe(false);
  });

  it("la modale annonce les verrous comme ignorés, pas comme des erreurs", async () => {
    const { container } = render(<DocumentsPage />);
    selectFolder(container, FOLDER);
    await screen.findByText("Dossier : D");

    expect(screen.getByText(/temporaires Office/i)).toBeInTheDocument();
    expect(screen.queryByText(/échec/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/corrompu/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/interdit/i)).not.toBeInTheDocument();
  });

  it("les verrous ne gonflent pas les compteurs par format", async () => {
    const { container } = render(<DocumentsPage />);
    selectFolder(container, FOLDER);
    await screen.findByText("Dossier : D");

    // 4 formats présents, 1 fichier chacun — pas 2.
    const boxes = screen.getAllByRole("checkbox");
    expect(boxes).toHaveLength(4);
    for (const cb of boxes) {
      expect(cb.getAttribute("aria-label") ?? cb.parentElement?.textContent ?? "").not.toMatch(
        /2 fichier/
      );
    }
  });

  it("un dossier de verrous UNIQUEMENT : import désactivé, aucun appel", async () => {
    const { container } = render(<DocumentsPage />);
    selectFolder(container, [fakeFile("D/~$a.docx"), fakeFile("D/~$b.xlsx")]);
    await screen.findByText("Dossier : D");

    const btn = screen.getByRole("button", { name: /Importer et indexer/i });
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(apiUploadForm).not.toHaveBeenCalled();
  });

  it("le bouton reste actif dès qu'un seul vrai document est présent", async () => {
    const { container } = render(<DocumentsPage />);
    selectFolder(container, [fakeFile("D/vrai.pdf"), fakeFile("D/~$vrai.pdf")]);
    await screen.findByText("Dossier : D");
    expect(screen.getByRole("button", { name: /Importer et indexer/i })).toBeEnabled();
  });

  it("le cas réel : 200 documents, 18 verrous, 200 appels", async () => {
    const files = [
      ...Array.from({ length: 200 }, (_, i) => fakeFile(`D/doc${i}.xlsx`)),
      ...Array.from({ length: 18 }, (_, i) => fakeFile(`D/~$doc${i}.xlsx`)),
    ];
    const { container } = render(<DocumentsPage />);
    selectFolder(container, files);
    await screen.findByText("Dossier : D");
    await confirmer();

    await waitFor(() => expect(apiUploadForm).toHaveBeenCalledTimes(200), { timeout: 15000 });
    expect(uploadedNames().some((n) => n.startsWith("~$"))).toBe(false);
  }, 20000);

  it("aucun DELETE : l'import reste strictement additif", async () => {
    const { container } = render(<DocumentsPage />);
    selectFolder(container, FOLDER);
    await screen.findByText("Dossier : D");
    await confirmer();
    await waitFor(() => expect(apiUploadForm).toHaveBeenCalledTimes(4));
    expect(deleteDocument).not.toHaveBeenCalled();
  });
});

describe("sélection de fichiers / glisser-déposer — aucun verrou n'atteint le réseau", () => {
  it("un verrou sélectionné manuellement n'est jamais envoyé", async () => {
    const { container } = render(<DocumentsPage />);
    selectFiles(container, [fakeFile("~$rapport.docx")]);
    await waitFor(() =>
      expect(screen.getByText(/Fichier temporaire créé par Microsoft Office/i)).toBeInTheDocument()
    );
    expect(apiUploadForm).not.toHaveBeenCalled();
  });

  it("il est affiché « Ignoré », jamais « Échec »", async () => {
    const { container } = render(<DocumentsPage />);
    selectFiles(container, [fakeFile("~$rapport.docx")]);
    await screen.findByText("Ignoré");
    expect(screen.queryByText("Échec")).not.toBeInTheDocument();
  });

  it("mélange : le vrai fichier part, le verrou non", async () => {
    const { container } = render(<DocumentsPage />);
    selectFiles(container, [fakeFile("rapport.docx"), fakeFile("~$rapport.docx")]);
    await waitFor(() => expect(apiUploadForm).toHaveBeenCalledTimes(1));
    expect(uploadedNames()).toEqual(["rapport.docx"]);
  });

  it("un vrai échec d'upload n'est pas mélangé aux verrous", async () => {
    apiUploadForm.mockRejectedValueOnce(new Error("boom"));
    const { container } = render(<DocumentsPage />);
    selectFiles(container, [fakeFile("casse.docx"), fakeFile("~$rapport.docx")]);

    await screen.findByText("Ignoré");
    await screen.findByText("Échec");
    // Deux lignes, deux statuts distincts : le verrou n'est pas compté comme une erreur.
    expect(screen.getByText("Ignoré")).toBeInTheDocument();
    expect(screen.getByText("Échec")).toBeInTheDocument();
  });

  it("le glisser-déposer d'un verrou n'envoie rien", async () => {
    const { container } = render(<DocumentsPage />);
    const dropzone = screen.getByText(/Glissez-déposez/i).closest("div") as HTMLElement;
    fireEvent.drop(dropzone, {
      dataTransfer: { files: [fakeFile("~$note.docx")] },
    });
    await waitFor(() =>
      expect(screen.getByText(/Fichier temporaire créé par Microsoft Office/i)).toBeInTheDocument()
    );
    expect(apiUploadForm).not.toHaveBeenCalled();
  });

  it("un fichier au tilde légitime part normalement", async () => {
    const { container } = render(<DocumentsPage />);
    selectFiles(container, [fakeFile("rapport~$.docx")]);
    await waitFor(() => expect(apiUploadForm).toHaveBeenCalledTimes(1));
    expect(uploadedNames()).toEqual(["rapport~$.docx"]);
  });
});
