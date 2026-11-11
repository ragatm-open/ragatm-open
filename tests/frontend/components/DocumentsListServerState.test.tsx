/**
 * Liste des documents — ce qui part sur le RÉSEAU et ce qui est affiché.
 *
 * Le défaut corrigé : l'écran chargeait 200 documents et présentait cette page
 * comme la totalité du RagSpace (compteurs `items.length`, recherche et filtres
 * locaux). Ces tests vérifient l'inverse — chaque critère est envoyé au serveur, et
 * les compteurs affichés sont ceux qu'il renvoie — en inspectant les paramètres de
 * requête, pas seulement le texte rendu.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/** URLs de liste réellement demandées, dans l'ordre. */
const listCalls: string[] = [];
/** Réponse de liste à servir (surchargeable par test). */
let listResponder: (url: string) => any;

const apiFetch = vi.fn(async (url: string) => {
  if (url.startsWith("/api/v1/documents/?")) {
    listCalls.push(url);
    return listResponder(url);
  }
  return {};
});

vi.mock("@/lib/api", () => ({
  apiFetch: (...a: any[]) => apiFetch(...(a as [string])),
  apiUploadForm: vi.fn(),
  apiFetchBlobUrl: vi.fn(),
  deleteDocument: vi.fn(),
  getCapabilities: vi.fn().mockResolvedValue({ embeddings: { provider: "ollama" } }),
  getWorkspaceQuota: vi.fn().mockResolvedValue({
    max_document_count: 5000,
    used_document_count: 1094,
    max_document_bytes: 100 * 1024 ** 3,
    used_document_bytes: 1024 ** 3,
    count_usage_percent: 21.9,
    size_usage_percent: 1,
    is_exceeded: false,
    can_upload: true,
  }),
  summarizeDocumentAwait: vi.fn(),
  summaryErrorKey: vi.fn(),
}));

vi.mock("@/components/shell", () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Topbar: ({ subtitle }: { subtitle?: string }) => <div data-testid="topbar">{subtitle}</div>,
  useWorkspace: () => ({
    ragSpace: { id: "SP1", name: "CM-2026", embedding_provider: "ollama" },
    ragSpaceId: "SP1",
    ragSpaceAccess: { permissionLevel: "write", spaceRole: null, isOwnerImplicit: false },
    canWriteCurrent: true,
    canManageCurrent: true,
    refreshStats: vi.fn(),
    workspaceId: 1,
  }),
}));

import DocumentsPage from "@/app/documents/page";

// --- Fabrique de réponses ---------------------------------------------------

const TOTAL = 237;
const COUNTS = { all: TOTAL, ready: 220, analyzing: 3, queued: 4, error: 10 };

function params(url: string) {
  return new URLSearchParams(url.split("?")[1] || "");
}

function makeItems(n: number, offset = 0) {
  return Array.from({ length: n }, (_, i) => ({
    id: `D${offset + i}`,
    rag_space_id: "SP1",
    filename: `document-${offset + i}.pdf`,
    content_type: "application/pdf",
    file_type: "PDF",
    size_bytes: 1024,
    sha256: "x",
    status: "READY",
    error_message: "",
    created_at: "2026-07-28T14:37:00Z",
    chunks_count: 2,
  }));
}

/** Réponse par défaut : un espace de 237 documents, paginé côté serveur. */
function defaultResponder(url: string) {
  const p = params(url);
  const limit = Number(p.get("limit") || 50);
  const offset = Number(p.get("offset") || 0);
  const total = p.get("status") === "error" ? COUNTS.error : TOTAL;
  return {
    items: makeItems(Math.max(0, Math.min(limit, total - offset)), offset),
    total,
    limit,
    offset,
    has_next: offset + limit < total,
    has_previous: offset > 0,
    ordering: p.get("ordering"),
    counts: COUNTS,
  };
}

const lastCall = () => listCalls[listCalls.length - 1];
const lastParams = () => params(lastCall());

/** Attend la requête N+1 (une nouvelle interaction a déclenché un rechargement). */
async function waitForCalls(n: number) {
  await waitFor(() => expect(listCalls.length).toBeGreaterThanOrEqual(n), { timeout: 3000 });
}

beforeEach(() => {
  listCalls.length = 0;
  listResponder = defaultResponder;
  apiFetch.mockClear();
});

// --- Compteurs et total -----------------------------------------------------

describe("Documents — le total et les compteurs viennent du serveur", () => {
  it("affiche le total du RagSpace, pas le nombre de lignes chargées", async () => {
    render(<DocumentsPage />);
    await waitForCalls(1);
    // 50 lignes rendues, 237 annoncés : c'est exactement ce que l'écran ne savait
    // pas dire avant ce lot.
    await waitFor(() => expect(screen.getAllByText(/document-\d+\.pdf/)).toHaveLength(50));
    expect(screen.getByTestId("topbar")).toHaveTextContent("237 fichiers");
    expect(screen.getByText("RagSpace : 237 documents")).toBeInTheDocument();
  });

  it("les onglets portent les compteurs de l'API", async () => {
    render(<DocumentsPage />);
    await waitForCalls(1);
    const tabs = await screen.findAllByRole("tab");
    const label = (name: string) => tabs.find((t) => t.textContent?.startsWith(name))!;
    await waitFor(() => expect(label("Tous")).toHaveTextContent("237"));
    expect(label("Prêts")).toHaveTextContent("220");
    expect(label("En analyse")).toHaveTextContent("3");
    expect(label("En attente")).toHaveTextContent("4");
    expect(label("Erreurs")).toHaveTextContent("10");
  });

  it("distingue le quota WORKSPACE du total du RagSpace", async () => {
    render(<DocumentsPage />);
    await waitForCalls(1);
    // 1 094 sur 5 000 = quota du workspace (tous espaces confondus) ; 237 = cet
    // espace. Les deux chiffres coexistent sans se contredire.
    // Ce test échouait en CI et passait en local. La cause n'était PAS le test :
    // `page.tsx` formatait ces nombres avec `toLocaleString()` SANS locale, donc
    // selon la locale du runtime — « 1 094 » sur un poste en `fr_FR.UTF-8`,
    // « 1,094 » sur un runner en locale `und`. Le défaut applicatif est corrigé
    // (`format.ts::count`) ; le test le verrouille désormais dans les deux sens.
    //
    // Le `waitFor` porte sur les CHIFFRES eux-mêmes, et non plus sur le libellé du
    // panneau : le quota vient d'un appel séparé que `waitForCalls` ne suit pas.
    await waitFor(() =>
      expect(screen.getByText(/Fichiers : 1\s*094 sur 5\s*000/)).toBeInTheDocument(),
    );
    expect(screen.getByText("Quota workspace")).toBeInTheDocument();
    expect(screen.getByText("RagSpace : 237 documents")).toBeInTheDocument();
  });

  it("un filtre de statut n'écrase pas les compteurs des autres onglets", async () => {
    render(<DocumentsPage />);
    await waitForCalls(1);
    fireEvent.click(await screen.findByRole("tab", { name: /Erreurs/ }));
    await waitForCalls(2);
    expect(lastParams().get("status")).toBe("error");
    const tabs = screen.getAllByRole("tab");
    expect(tabs.find((t) => t.textContent?.startsWith("Prêts"))).toHaveTextContent("220");
  });
});

// --- Requêtes serveur -------------------------------------------------------

describe("Documents — filtres, recherche et tri sont exécutés par le serveur", () => {
  it("le premier chargement demande une page bornée, triée par défaut", async () => {
    render(<DocumentsPage />);
    await waitForCalls(1);
    const p = lastParams();
    expect(p.get("rag_space_id")).toBe("SP1");
    expect(p.get("limit")).toBe("50");
    expect(p.get("offset")).toBe("0");
    expect(p.get("ordering")).toBe("-created_at");
    expect(p.get("status")).toBeNull();
    expect(lastCall()).not.toContain("limit=200");
  });

  it("changer d'onglet envoie le statut au serveur", async () => {
    render(<DocumentsPage />);
    await waitForCalls(1);
    fireEvent.click(await screen.findByRole("tab", { name: /En analyse/ }));
    await waitForCalls(2);
    expect(lastParams().get("status")).toBe("analyzing");
  });

  it("la recherche part sur le réseau (temporisée) et non en local", async () => {
    render(<DocumentsPage />);
    await waitForCalls(1);
    fireEvent.change(screen.getByPlaceholderText(/Rechercher un document/), {
      target: { value: "rapport" },
    });
    await waitFor(() => expect(lastParams().get("search")).toBe("rapport"), { timeout: 3000 });
  });

  it("la recherche ne déclenche pas une requête par frappe", async () => {
    render(<DocumentsPage />);
    await waitForCalls(1);
    const input = screen.getByPlaceholderText(/Rechercher un document/);
    for (const v of ["r", "ra", "rap", "rapp", "rappo", "rappor", "rapport"]) {
      fireEvent.change(input, { target: { value: v } });
    }
    await waitFor(() => expect(lastParams().get("search")).toBe("rapport"), { timeout: 3000 });
    const searched = listCalls.filter((u) => params(u).get("search"));
    expect(searched.length).toBeLessThanOrEqual(2);
  });

  it("la recherche revient à la première page", async () => {
    render(<DocumentsPage />);
    await waitForCalls(1);
    fireEvent.click(await screen.findByRole("button", { name: "Suivant" }));
    await waitFor(() => expect(lastParams().get("offset")).toBe("50"));
    fireEvent.change(screen.getByPlaceholderText(/Rechercher un document/), {
      target: { value: "budget" },
    });
    await waitFor(() => expect(lastParams().get("search")).toBe("budget"), { timeout: 3000 });
    expect(lastParams().get("offset")).toBe("0");
  });

  it.each([
    ["Document", "filename", "filename"],
    ["Type", "file_type", "file_type"],
    ["Statut", "status", "status"],
  ])("le tri par %s est demandé au serveur, puis inversé", async (label, _f, field) => {
    render(<DocumentsPage />);
    await waitForCalls(1);
    const header = await screen.findByRole("button", { name: new RegExp(`^${label}$`) });
    fireEvent.click(header);
    await waitFor(() => expect(lastParams().get("ordering")).toBe(field));
    fireEvent.click(header);
    await waitFor(() => expect(lastParams().get("ordering")).toBe(`-${field}`));
  });

  it("le tri par date part du plus récent puis s'inverse", async () => {
    render(<DocumentsPage />);
    await waitForCalls(1);
    // La colonne date est DÉJÀ le tri actif : le premier clic doit l'inverser.
    const header = await screen.findByRole("button", { name: /^Importé le$/ });
    fireEvent.click(header);
    await waitFor(() => expect(lastParams().get("ordering")).toBe("created_at"));
    fireEvent.click(header);
    await waitFor(() => expect(lastParams().get("ordering")).toBe("-created_at"));
  });

  it("changer de tri revient à la première page", async () => {
    render(<DocumentsPage />);
    await waitForCalls(1);
    fireEvent.click(await screen.findByRole("button", { name: "Suivant" }));
    await waitFor(() => expect(lastParams().get("offset")).toBe("50"));
    fireEvent.click(screen.getByRole("button", { name: /^Document$/ }));
    await waitFor(() => expect(lastParams().get("ordering")).toBe("filename"));
    expect(lastParams().get("offset")).toBe("0");
  });

  it("le sélecteur « Trier par » pilote le même état que les en-têtes", async () => {
    render(<DocumentsPage />);
    await waitForCalls(1);
    fireEvent.change(screen.getByLabelText("Trier par"), { target: { value: "filename" } });
    await waitFor(() => expect(lastParams().get("ordering")).toBe("filename"));
    const header = screen.getByRole("button", { name: /^Document$/ });
    expect(header.closest("[role=columnheader]")).toHaveAttribute("aria-sort", "ascending");
  });
});

// --- Pagination -------------------------------------------------------------

describe("Documents — pagination serveur", () => {
  it("affiche la plage réelle et navigue de page en page", async () => {
    render(<DocumentsPage />);
    await waitForCalls(1);
    expect(await screen.findByText("1–50 sur 237 documents")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Suivant" }));
    await waitFor(() => expect(lastParams().get("offset")).toBe("50"));
    expect(await screen.findByText("51–100 sur 237 documents")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Précédent" }));
    await waitFor(() => expect(lastParams().get("offset")).toBe("0"));
  });

  it("« Précédent » est inactif sur la première page", async () => {
    render(<DocumentsPage />);
    await waitForCalls(1);
    expect(await screen.findByRole("button", { name: "Précédent" })).toBeDisabled();
  });

  it("changer la taille de page recharge depuis le début", async () => {
    render(<DocumentsPage />);
    await waitForCalls(1);
    fireEvent.click(screen.getByRole("button", { name: "Suivant" }));
    await waitFor(() => expect(lastParams().get("offset")).toBe("50"));
    fireEvent.change(screen.getByLabelText("Par page"), { target: { value: "100" } });
    await waitFor(() => expect(lastParams().get("limit")).toBe("100"));
    expect(lastParams().get("offset")).toBe("0");
  });
});

// --- Courses réseau ---------------------------------------------------------

describe("Documents — une réponse obsolète n'écrase pas la plus récente", () => {
  it("la réponse lente d'une recherche abandonnée est ignorée", async () => {
    // La 2ᵉ requête (recherche) répond AVANT la 1ʳᵉ (chargement initial lent).
    let releaseFirst: (v: any) => void = () => {};
    let n = 0;
    listResponder = (url: string) => {
      n += 1;
      if (n === 1) {
        return new Promise((resolve) => {
          releaseFirst = () => resolve({ ...defaultResponder(url), counts: { ...COUNTS, all: 999 } });
        });
      }
      return { ...defaultResponder(url), counts: { ...COUNTS, all: 42 } };
    };

    render(<DocumentsPage />);
    await waitForCalls(1);
    fireEvent.change(screen.getByPlaceholderText(/Rechercher un document/), {
      target: { value: "rapport" },
    });
    await waitFor(() => expect(listCalls.length).toBe(2), { timeout: 3000 });
    await waitFor(() => expect(screen.getByTestId("topbar")).toHaveTextContent("42 fichiers"));

    releaseFirst(null);
    // La réponse tardive de la requête abandonnée ne doit rien réécrire.
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.getByTestId("topbar")).toHaveTextContent("42 fichiers");
  });
});

// --- Rendu ------------------------------------------------------------------

describe("Documents — rendu de la liste", () => {
  it("affiche la date ET l'heure d'import", async () => {
    render(<DocumentsPage />);
    await waitForCalls(1);
    // Format localisé fr produit par `Intl` (jamais concaténé à la main) :
    // « 28 juil. 2026, 16:37 » — l'heure locale du runner peut varier, pas le motif.
    const cells = await screen.findAllByText(/28 juil\. 2026,? \d{2}:\d{2}/);
    expect(cells.length).toBeGreaterThan(0);
  });

  it("affiche le type décidé par le serveur dans une colonne dédiée", async () => {
    listResponder = (url: string) => ({
      ...defaultResponder(url),
      items: [
        { ...makeItems(1)[0], id: "A", filename: "photo.jpg", content_type: "image/jpeg", file_type: "JPEG" },
      ],
      total: 1,
      counts: { ...COUNTS, all: 1 },
    });
    render(<DocumentsPage />);
    await waitForCalls(1);
    // `file_type` du backend (JPEG) prime sur l'extension du nom (JPG) : c'est la
    // valeur sur laquelle le serveur trie.
    await waitFor(() => expect(screen.getAllByText("JPEG").length).toBeGreaterThan(0));
    expect(screen.queryByText("JPG")).not.toBeInTheDocument();
  });

  it("les en-têtes triables exposent aria-sort et sont actionnables au clavier", async () => {
    render(<DocumentsPage />);
    await waitForCalls(1);
    const headers = await screen.findAllByRole("columnheader");
    const dateHeader = headers.find((h) => h.textContent?.includes("Importé le"))!;
    expect(dateHeader).toHaveAttribute("aria-sort", "descending");
    const nameHeader = headers.find((h) => h.textContent?.includes("Document"))!;
    expect(nameHeader).toHaveAttribute("aria-sort", "none");

    const button = within(nameHeader).getByRole("button");
    button.focus();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(lastParams().get("ordering")).toBe("filename"));
    expect(nameHeader).toHaveAttribute("aria-sort", "ascending");
  });

  it("après suppression, la liste est rechargée depuis le serveur", async () => {
    render(<DocumentsPage />);
    await waitForCalls(1);
    const rows = await screen.findAllByRole("button", { name: "Actions du document" });
    fireEvent.click(rows[0]);
    fireEvent.click(await screen.findByRole("menuitem", { name: /Supprimer définitivement/ }));
    const dialogButtons = await screen.findAllByRole("button", { name: /Supprimer définitivement/ });
    fireEvent.click(dialogButtons[dialogButtons.length - 1]);
    // Retirer la ligne localement laisserait `total` et les compteurs périmés.
    await waitForCalls(2);
  });

  it("un filtre sans résultat le dit sans proposer d'importer", async () => {
    listResponder = (url: string) => ({
      ...defaultResponder(url),
      items: [],
      total: 0,
      counts: { ...COUNTS, error: 0 },
    });
    render(<DocumentsPage />);
    await waitForCalls(1);
    fireEvent.click(await screen.findByRole("tab", { name: /Erreurs/ }));
    await waitForCalls(2);
    expect(await screen.findByText(/Aucun document avec le statut/)).toBeInTheDocument();
  });
});
