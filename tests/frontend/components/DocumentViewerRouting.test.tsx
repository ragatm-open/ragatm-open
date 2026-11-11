import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

/**
 * Routage du visualiseur `/doc/[documentId]` — garantie centrale du lot XLSX :
 * un document n'est JAMAIS chargé dans une iframe avant que son type ne soit connu,
 * et aucun état ne produit une grande zone blanche.
 */

const replace = vi.fn();
const push = vi.fn();
let searchParams = new URLSearchParams();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push }),
  useParams: () => ({ documentId: "doc-1" }),
  useSearchParams: () => searchParams,
}));

const apiGet = vi.fn();
const apiFetchBlobUrl = vi.fn();
vi.mock("@/lib/api", () => ({
  apiGet: (...a: unknown[]) => apiGet(...a),
  apiFetchBlobUrl: (...a: unknown[]) => apiFetchBlobUrl(...a),
}));

// Les visualiseurs ont leurs propres tests ; ici on vérifie qu'ils sont CHOISIS.
vi.mock("@/app/doc/[documentId]/_components/SpreadsheetDocumentViewer", () => ({
  SpreadsheetDocumentViewer: (p: { documentId: string; chunkId: string | null }) => (
    <div data-testid="spreadsheet-viewer" data-chunk={p.chunkId ?? ""}>
      grille
    </div>
  ),
}));
vi.mock("@/app/doc/[documentId]/_components/PresentationPreviewViewer", () => ({
  PresentationPreviewViewer: (p: { documentId: string; chunkId: string | null }) => (
    <div data-testid="presentation-viewer" data-chunk={p.chunkId ?? ""}>
      diapositives
    </div>
  ),
}));
vi.mock("@/app/doc/[documentId]/_components/DocumentPreviewViewer", () => ({
  DocumentPreviewViewer: (p: { documentId: string; chunkId: string | null }) => (
    <div data-testid="document-viewer" data-chunk={p.chunkId ?? ""}>
      blocs
    </div>
  ),
}));

import DocumentViewerPage from "@/app/doc/[documentId]/page";

const XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
const DOCX =
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
const PPTX =
  "application/vnd.openxmlformats-officedocument.presentationml.presentation";

beforeEach(() => {
  vi.clearAllMocks();
  searchParams = new URLSearchParams();
  apiFetchBlobUrl.mockResolvedValue({
    url: "blob:fake",
    revoke: vi.fn(),
    contentType: "application/pdf",
  });
});
afterEach(() => vi.clearAllMocks());

describe("routage par type", () => {
  it("PDF → iframe, blob et #page=N conservés (non-régression)", async () => {
    searchParams = new URLSearchParams("page=3");
    apiGet.mockResolvedValue({
      id: "doc-1",
      filename: "rapport.pdf",
      content_type: "application/pdf",
      preview_kind: "pdf",
    });

    const { container } = render(<DocumentViewerPage />);

    await waitFor(() => expect(container.querySelector("iframe")).toBeTruthy());
    const iframe = container.querySelector("iframe")!;
    expect(iframe.getAttribute("src")).toBe("blob:fake#page=3");

    // Le téléchargement du fichier n'a lieu qu'APRÈS la résolution du type.
    expect(apiGet).toHaveBeenCalledWith("/api/v1/documents/doc-1/");
    expect(apiFetchBlobUrl).toHaveBeenCalledWith(
      "/api/v1/documents/doc-1/download/",
      expect.objectContaining({ method: "GET" })
    );
  });

  it("XLSX → visualiseur tabulaire, jamais d'iframe", async () => {
    searchParams = new URLSearchParams("chunk_id=chunk-42");
    apiGet.mockResolvedValue({
      id: "doc-1",
      filename: "REFERENTIEL.xlsx",
      content_type: XLSX,
      preview_kind: "spreadsheet",
    });

    const { container } = render(<DocumentViewerPage />);

    await waitFor(() =>
      expect(screen.getByTestId("spreadsheet-viewer")).toBeInTheDocument()
    );
    expect(container.querySelector("iframe")).toBeNull();
    // Aucun octet du classeur n'est téléchargé par la page.
    expect(apiFetchBlobUrl).not.toHaveBeenCalled();
    // Le chunk_id est TRANSMIS au visualiseur : c'est lui qui localise la source.
    expect(screen.getByTestId("spreadsheet-viewer").getAttribute("data-chunk")).toBe(
      "chunk-42"
    );
  });

  it("DOCX → visualiseur documentaire, jamais d'iframe", async () => {
    searchParams = new URLSearchParams("chunk_id=chunk-7");
    apiGet.mockResolvedValue({
      id: "doc-1",
      filename: "rapport.docx",
      content_type: DOCX,
      preview_kind: "document",
    });

    const { container } = render(<DocumentViewerPage />);

    await waitFor(() =>
      expect(screen.getByTestId("document-viewer")).toBeInTheDocument()
    );
    expect(container.querySelector("iframe")).toBeNull();
    // Aucun octet du document n'est téléchargé par la page.
    expect(apiFetchBlobUrl).not.toHaveBeenCalled();
    expect(screen.getByTestId("document-viewer").getAttribute("data-chunk")).toBe(
      "chunk-7"
    );
  });

  it("DOCX avec drapeau fermé → état explicite (décision backend)", async () => {
    apiGet.mockResolvedValue({
      id: "doc-1",
      filename: "rapport.docx",
      content_type: DOCX,
      preview_kind: "unsupported",
    });

    render(<DocumentViewerPage />);
    await waitFor(() =>
      expect(screen.getByText("Aperçu intégré indisponible")).toBeInTheDocument()
    );
    expect(screen.queryByTestId("document-viewer")).toBeNull();
  });

  it("PPTX → visualiseur de présentation, jamais d'iframe", async () => {
    searchParams = new URLSearchParams("chunk_id=chunk-9");
    apiGet.mockResolvedValue({
      id: "doc-1",
      filename: "architecture.pptx",
      content_type: PPTX,
      preview_kind: "presentation",
    });

    const { container } = render(<DocumentViewerPage />);

    await waitFor(() =>
      expect(screen.getByTestId("presentation-viewer")).toBeInTheDocument()
    );
    expect(container.querySelector("iframe")).toBeNull();
    expect(apiFetchBlobUrl).not.toHaveBeenCalled();
    expect(
      screen.getByTestId("presentation-viewer").getAttribute("data-chunk")
    ).toBe("chunk-9");
  });

  it("PPTX avec drapeau fermé → état explicite (décision backend)", async () => {
    apiGet.mockResolvedValue({
      id: "doc-1",
      filename: "architecture.pptx",
      content_type: PPTX,
      preview_kind: "unsupported",
    });

    render(<DocumentViewerPage />);
    await waitFor(() =>
      expect(screen.getByText("Aperçu intégré indisponible")).toBeInTheDocument()
    );
    expect(screen.queryByTestId("presentation-viewer")).toBeNull();
  });

  it("CSV → état explicite, pas d'iframe, téléchargement proposé", async () => {
    apiGet.mockResolvedValue({
      id: "doc-1",
      filename: "export.csv",
      content_type: "text/csv",
      preview_kind: "unsupported",
    });

    const { container } = render(<DocumentViewerPage />);

    await waitFor(() =>
      expect(screen.getByText("Aperçu intégré indisponible")).toBeInTheDocument()
    );
    expect(container.querySelector("iframe")).toBeNull();
    expect(screen.getByText("CSV")).toBeInTheDocument();
    // Deux points d'action : la barre d'outils persistante et l'appel à l'action
    // de l'état explicite. Les deux mènent au téléchargement de l'original.
    expect(
      screen.getAllByRole("button", { name: /Télécharger l'original/ }).length
    ).toBeGreaterThanOrEqual(1);
  });

  it("format inconnu → état explicite", async () => {
    apiGet.mockResolvedValue({
      id: "doc-1",
      filename: "chose.bin",
      content_type: "application/x-unknown",
      preview_kind: "unsupported",
    });

    render(<DocumentViewerPage />);
    await waitFor(() =>
      expect(screen.getByText("Aperçu intégré indisponible")).toBeInTheDocument()
    );
  });

  it("backend sans preview_kind → repli sur le type MIME", async () => {
    apiGet.mockResolvedValue({
      id: "doc-1",
      filename: "REFERENTIEL.xlsx",
      content_type: XLSX,
    });

    render(<DocumentViewerPage />);
    await waitFor(() =>
      expect(screen.getByTestId("spreadsheet-viewer")).toBeInTheDocument()
    );
  });
});

describe("états d'erreur — jamais de zone blanche", () => {
  it("404 sur les métadonnées → message explicite et actionnable", async () => {
    apiGet.mockRejectedValue(new Error("HTTP 404 not_found"));

    render(<DocumentViewerPage />);
    await waitFor(() =>
      expect(
        screen.getByText(/introuvable, ou vous n'y avez pas accès/)
      ).toBeInTheDocument()
    );
    expect(
      screen.getAllByRole("button", { name: /Télécharger l'original/ }).length
    ).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByRole("button", { name: /Retour/ }).length).toBeGreaterThanOrEqual(1);
  });

  it("401 → redirection login, aucun rendu de document", async () => {
    apiGet.mockRejectedValue(new Error("HTTP 401 unauthorized"));

    render(<DocumentViewerPage />);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
  });

  it("erreur réseau → message explicite, pas de vide", async () => {
    apiGet.mockRejectedValue(new Error("Failed to fetch"));

    render(<DocumentViewerPage />);
    await waitFor(() =>
      expect(screen.getByText(/Impossible de charger les informations/)).toBeInTheDocument()
    );
  });

  it("pendant le chargement, un état est annoncé", async () => {
    apiGet.mockImplementation(() => new Promise(() => {}));
    render(<DocumentViewerPage />);
    expect(screen.getByText("Chargement du document…")).toBeInTheDocument();
  });
});
