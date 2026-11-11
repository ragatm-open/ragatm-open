/**
 * Reprise en masse des documents en erreur — sélection, payload, suivi du job.
 *
 * Ces tests portent sur ce qui part au serveur et sur ce que l'écran promet. Deux
 * pièges y sont verrouillés en particulier : confondre « les 50 de cette page »
 * avec « les 184 du filtre », et présenter un job qui démarre comme un job
 * terminé (le défaut du bouton global hérité du lot 1).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const listCalls: string[] = [];
const postCalls: Array<{ url: string; body: any }> = [];
let listResponder: (url: string) => any;
let rawResponder: (url: string, init: any) => { status: number; body: any };
let jobResponder: (url: string) => any;

const apiFetch = vi.fn(async (url: string) => {
  if (url.startsWith("/api/v1/documents/?")) {
    listCalls.push(url);
    return listResponder(url);
  }
  if (url.includes("/reindex-jobs/") || url.includes("/active-reindex-job/")) {
    return jobResponder(url);
  }
  return {};
});

/** Implémentation par défaut : enregistre l'appel puis délègue à `rawResponder`. */
async function defaultRawImpl(url: string, init: any = {}) {
  const parsed = init?.body ? JSON.parse(init.body) : null;
  postCalls.push({ url, body: parsed });
  const { status, body } = rawResponder(url, init);
  return { status, json: async () => body } as any;
}

const apiFetchRaw = vi.fn(defaultRawImpl);

vi.mock("@/lib/api", () => ({
  apiFetch: (...a: any[]) => apiFetch(...(a as [string])),
  apiFetchRaw: (...a: any[]) => apiFetchRaw(...(a as [string, any])),
  apiUploadForm: vi.fn(),
  apiFetchBlobUrl: vi.fn(),
  deleteDocument: vi.fn(),
  getCapabilities: vi.fn().mockResolvedValue({ embeddings: { provider: "ollama" } }),
  getWorkspaceQuota: vi.fn().mockResolvedValue({
    max_document_count: 5000, used_document_count: 1094,
    max_document_bytes: 1024 ** 4, used_document_bytes: 1024 ** 3,
    count_usage_percent: 21, size_usage_percent: 1, is_exceeded: false, can_upload: true,
  }),
  summarizeDocumentAwait: vi.fn(),
  summaryErrorKey: vi.fn(),
}));

let canManage = true;
vi.mock("@/components/shell", () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Topbar: ({ subtitle }: { subtitle?: string }) => <div data-testid="topbar">{subtitle}</div>,
  useWorkspace: () => ({
    ragSpace: { id: "SP1", name: "CM-2026", embedding_provider: "ollama" },
    ragSpaceId: "SP1",
    ragSpaceAccess: { permissionLevel: "write", spaceRole: null, isOwnerImplicit: false },
    canWriteCurrent: true,
    get canManageCurrent() {
      return canManage;
    },
    refreshStats: vi.fn(),
    workspaceId: 1,
  }),
}));

import { __resetUploadScheduler } from "@/app/documents/_lib/uploadScheduler";
import DocumentsPage from "@/app/documents/page";

// --- Données ----------------------------------------------------------------

const ERROR_TOTAL = 184;
const COUNTS = { all: 237, ready: 50, analyzing: 0, queued: 3, error: ERROR_TOTAL };

function params(url: string) {
  return new URLSearchParams(url.split("?")[1] || "");
}

function errorItems(n: number, offset = 0) {
  return Array.from({ length: n }, (_, i) => ({
    id: `E${offset + i}`,
    rag_space_id: "SP1",
    filename: `echec-${offset + i}.pdf`,
    content_type: "application/pdf",
    file_type: "PDF",
    size_bytes: 100,
    sha256: "x",
    status: "FAILED",
    error_message: "extraction_timeout: …",
    created_at: "2026-07-28T14:37:00Z",
    chunks_count: 0,
  }));
}

function defaultResponder(url: string) {
  const p = params(url);
  const limit = Number(p.get("limit") || 50);
  const offset = Number(p.get("offset") || 0);
  const isError = p.get("status") === "error";
  const total = isError ? ERROR_TOTAL : COUNTS.all;
  return {
    items: (isError ? errorItems : errorItems)(Math.max(0, Math.min(limit, total - offset)), offset),
    total,
    limit,
    offset,
    has_next: offset + limit < total,
    has_previous: offset > 0,
    ordering: p.get("ordering"),
    counts: COUNTS,
  };
}

function job(overrides: any = {}) {
  return {
    job_id: "JOB1",
    rag_space_id: "SP1",
    status: "running",
    kind: "failed_documents_retry",
    selection: { mode: "filter", eligible_count: 184 },
    total: 184, pending: 100, queued: 0, running: 4, succeeded: 78, failed: 2,
    cancelled: 0, interrupted: 0, progress_percent: 43.5,
    parent_job_id: null, error_code: null,
    started_at: null, finished_at: null, created_at: null, updated_at: null,
    ...overrides,
  };
}

const lastPost = () => postCalls[postCalls.length - 1];

async function waitForListCalls(n: number) {
  await waitFor(() => expect(listCalls.length).toBeGreaterThanOrEqual(n), { timeout: 3000 });
}

/** Ouvre l'onglet Erreurs et attend la requête serveur correspondante. */
async function openErrorsTab() {
  render(<DocumentsPage />);
  await waitForListCalls(1);
  fireEvent.click(await screen.findByRole("tab", { name: /Erreurs/ }));
  await waitFor(() => expect(params(listCalls[listCalls.length - 1]).get("status")).toBe("error"));
  await screen.findAllByRole("checkbox");
}

beforeEach(() => {
  // La cadence d'envoi est PARTAGÉE au niveau du module (un onglet = une
  // cadence). Sans remise à zéro, le créneau réservé par un test repousse le
  // premier envoi du suivant et le fait expirer.
  __resetUploadScheduler();
  listCalls.length = 0;
  postCalls.length = 0;
  canManage = true;
  listResponder = defaultResponder;
  jobResponder = () => ({ active: false, job: null });
  rawResponder = () => ({
    status: 202,
    body: { status: "accepted", job_id: "JOB1", kind: "failed_documents_retry", total_documents: 184, already_running: false },
  });
  apiFetch.mockClear();
  // `mockClear` n'efface PAS une implémentation posée par un test (cf. le cas du
  // double clic) : on la remet explicitement, sinon elle contamine la suite.
  apiFetchRaw.mockReset();
  apiFetchRaw.mockImplementation(defaultRawImpl);
});

// --- Sélection --------------------------------------------------------------

describe("Sélection des documents en erreur", () => {
  it("les cases n'apparaissent QUE dans l'onglet Erreurs", async () => {
    render(<DocumentsPage />);
    await waitForListCalls(1);
    await screen.findAllByText(/echec-\d+\.pdf/);
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);

    fireEvent.click(screen.getByRole("tab", { name: /Erreurs/ }));
    await waitFor(() => expect(screen.getAllByRole("checkbox").length).toBeGreaterThan(0));
  });

  it("les cases sont absentes sans droit de gestion", async () => {
    canManage = false;
    render(<DocumentsPage />);
    await waitForListCalls(1);
    fireEvent.click(await screen.findByRole("tab", { name: /Erreurs/ }));
    await waitFor(() => expect(params(listCalls[listCalls.length - 1]).get("status")).toBe("error"));
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
  });

  it("cocher une ligne affiche le compteur et l'action", async () => {
    await openErrorsTab();
    fireEvent.click(screen.getByRole("checkbox", { name: /Sélectionner echec-0\.pdf/ }));
    expect(await screen.findByText("1 document sélectionné")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Réingérer la sélection" })).toBeInTheDocument();
  });

  it("la case d'en-tête sélectionne la PAGE, et le dit", async () => {
    await openErrorsTab();
    fireEvent.click(screen.getByRole("checkbox", { name: /Sélectionner les documents de cette page/ }));
    // 50 lignes cochées sur 184 en erreur : l'écart doit être explicite.
    expect(await screen.findByText("50 documents sélectionnés")).toBeInTheDocument();
    expect(screen.getByText(/cette page uniquement/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Sélectionner les 184 documents en erreur" })
    ).toBeInTheDocument();
  });

  it("l'extension à tous les résultats change le compteur", async () => {
    await openErrorsTab();
    fireEvent.click(screen.getByRole("checkbox", { name: /Sélectionner les documents de cette page/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Sélectionner les 184/ }));
    expect(await screen.findByText("184 documents sélectionnés")).toBeInTheDocument();
    expect(screen.getByText(/Tous les documents en erreur du filtre courant/)).toBeInTheDocument();
  });

  it("décocher une ligne d'une sélection globale la réduit d'un", async () => {
    await openErrorsTab();
    fireEvent.click(screen.getByRole("checkbox", { name: /Sélectionner les documents de cette page/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Sélectionner les 184/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /Sélectionner echec-3\.pdf/ }));
    expect(await screen.findByText("183 documents sélectionnés")).toBeInTheDocument();
  });

  it("la sélection survit au changement de page", async () => {
    await openErrorsTab();
    fireEvent.click(screen.getByRole("checkbox", { name: /Sélectionner echec-0\.pdf/ }));
    await screen.findByText("1 document sélectionné");
    fireEvent.click(screen.getByRole("button", { name: "Suivant" }));
    await waitFor(() => expect(params(listCalls[listCalls.length - 1]).get("offset")).toBe("50"));
    // Une sélection par identifiant ne dépend pas des lignes affichées.
    expect(screen.getByText("1 document sélectionné")).toBeInTheDocument();
  });

  it("la sélection survit au changement de tri", async () => {
    await openErrorsTab();
    fireEvent.click(screen.getByRole("checkbox", { name: /Sélectionner echec-0\.pdf/ }));
    await screen.findByText("1 document sélectionné");
    fireEvent.click(screen.getByRole("button", { name: /^Document$/ }));
    await waitFor(() => expect(params(listCalls[listCalls.length - 1]).get("ordering")).toBe("filename"));
    expect(screen.getByText("1 document sélectionné")).toBeInTheDocument();
  });

  it("la sélection est vidée par un changement de recherche", async () => {
    await openErrorsTab();
    fireEvent.click(screen.getByRole("checkbox", { name: /Sélectionner echec-0\.pdf/ }));
    await screen.findByText("1 document sélectionné");
    fireEvent.change(screen.getByPlaceholderText(/Rechercher un document/), {
      target: { value: "rapport" },
    });
    // « les 184 en erreur » ne veut plus rien dire une fois le périmètre changé.
    await waitFor(() => expect(screen.queryByText(/document sélectionné/)).not.toBeInTheDocument(), {
      timeout: 3000,
    });
  });

  it("la sélection est vidée en quittant l'onglet Erreurs", async () => {
    await openErrorsTab();
    fireEvent.click(screen.getByRole("checkbox", { name: /Sélectionner echec-0\.pdf/ }));
    await screen.findByText("1 document sélectionné");
    fireEvent.click(screen.getByRole("tab", { name: /^Tous/ }));
    await waitFor(() => expect(screen.queryByText(/document sélectionné/)).not.toBeInTheDocument());
  });

  it("« Effacer la sélection » remet à zéro", async () => {
    await openErrorsTab();
    fireEvent.click(screen.getByRole("checkbox", { name: /Sélectionner echec-0\.pdf/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Effacer la sélection" }));
    await waitFor(() => expect(screen.queryByText(/document sélectionné/)).not.toBeInTheDocument());
  });

  it("les cases sont actionnables au clavier", async () => {
    await openErrorsTab();
    const cb = screen.getByRole("checkbox", { name: /Sélectionner echec-1\.pdf/ });
    cb.focus();
    await userEvent.keyboard(" ");
    expect(await screen.findByText("1 document sélectionné")).toBeInTheDocument();
  });
});

// --- Lancement --------------------------------------------------------------

describe("Lancement de la reprise", () => {
  async function selectAndOpenModal() {
    await openErrorsTab();
    fireEvent.click(screen.getByRole("checkbox", { name: /Sélectionner echec-0\.pdf/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Réingérer la sélection" }));
    await screen.findByRole("button", { name: "Lancer la reprise" });
  }

  it("une confirmation précède tout envoi", async () => {
    await openErrorsTab();
    fireEvent.click(screen.getByRole("checkbox", { name: /Sélectionner echec-0\.pdf/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Réingérer la sélection" }));
    expect(await screen.findByText(/Réingérer 1 document en erreur/)).toBeInTheDocument();
    // La modale dit ce que l'opération N'EST PAS.
    expect(screen.getByText(/Aucun nouveau document n'est créé/)).toBeInTheDocument();
    // Depuis le lot 3, elle propose aussi une méthode — `auto` cochée par défaut.
    expect(screen.getByRole("radio", { name: /Automatique/ })).toBeChecked();
    expect(postCalls).toHaveLength(0);
  });

  it("annuler n'envoie rien", async () => {
    await selectAndOpenModal();
    fireEvent.click(screen.getByRole("button", { name: "Annuler" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Lancer la reprise" })).not.toBeInTheDocument());
    expect(postCalls).toHaveLength(0);
  });

  it("payload de sélection EXPLICITE", async () => {
    await selectAndOpenModal();
    fireEvent.click(screen.getByRole("button", { name: "Lancer la reprise" }));
    await waitFor(() => expect(postCalls).toHaveLength(1));
    expect(lastPost().url).toBe("/api/v1/rag-spaces/SP1/retry-failed-documents/");
    expect(lastPost().body).toEqual({
      selection: { mode: "explicit", document_ids: ["E0"] },
      // `auto` par défaut depuis le lot 3 : la méthode suit la cause de l'échec.
      strategy: "auto",
    });
  });

  it("payload de sélection PAR FILTRE (jamais 184 identifiants)", async () => {
    await openErrorsTab();
    fireEvent.change(screen.getByPlaceholderText(/Rechercher un document/), {
      target: { value: "rapport" },
    });
    await waitFor(() => expect(params(listCalls[listCalls.length - 1]).get("search")).toBe("rapport"), {
      timeout: 3000,
    });
    fireEvent.click(screen.getByRole("checkbox", { name: /Sélectionner les documents de cette page/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Sélectionner les 184/ }));
    fireEvent.click(screen.getByRole("button", { name: "Réingérer la sélection" }));
    fireEvent.click(await screen.findByRole("button", { name: "Lancer la reprise" }));

    await waitFor(() => expect(postCalls).toHaveLength(1));
    expect(lastPost().body).toEqual({
      selection: { mode: "filter", search: "rapport", excluded_document_ids: [] },
      strategy: "auto",
    });
  });

  it("les exclusions voyagent avec le filtre", async () => {
    await openErrorsTab();
    fireEvent.click(screen.getByRole("checkbox", { name: /Sélectionner les documents de cette page/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Sélectionner les 184/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /Sélectionner echec-2\.pdf/ }));
    fireEvent.click(screen.getByRole("button", { name: "Réingérer la sélection" }));
    fireEvent.click(await screen.findByRole("button", { name: "Lancer la reprise" }));

    await waitFor(() => expect(postCalls).toHaveLength(1));
    expect(lastPost().body.selection.excluded_document_ids).toEqual(["E2"]);
  });

  it("un double clic ne lance qu'un seul job", async () => {
    let release: (v: any) => void = () => {};
    rawResponder = () => ({ status: 202, body: {} });
    apiFetchRaw.mockImplementation(async (url: string, init: any = {}) => {
      postCalls.push({ url, body: init?.body ? JSON.parse(init.body) : null });
      return new Promise((resolve) => {
        release = () =>
          resolve({
            status: 202,
            json: async () => ({ status: "accepted", job_id: "JOB1", total_documents: 1, already_running: false }),
          } as any);
      });
    });

    await selectAndOpenModal();
    const btn = screen.getByRole("button", { name: "Lancer la reprise" });
    fireEvent.click(btn);
    fireEvent.click(btn);
    fireEvent.click(btn);
    await waitFor(() => expect(postCalls).toHaveLength(1));
    release(null);
  });

  it("202 ouvre le suivi du job", async () => {
    jobResponder = () => job();
    await selectAndOpenModal();
    fireEvent.click(screen.getByRole("button", { name: "Lancer la reprise" }));
    expect(await screen.findByText("Reprise des documents en erreur")).toBeInTheDocument();
    expect(screen.getByText(/78\/184 terminés/)).toBeInTheDocument();
  });

  it("409 « déjà en cours » adopte le job existant au lieu d'échouer", async () => {
    rawResponder = () => ({
      status: 409,
      body: {
        status: "already_running",
        job_id: "JOB-EXISTING",
        already_running: true,
        job: job({ job_id: "JOB-EXISTING", kind: "full_reindex" }),
      },
    });
    await selectAndOpenModal();
    fireEvent.click(screen.getByRole("button", { name: "Lancer la reprise" }));
    expect(await screen.findByText("Réindexation de l'espace")).toBeInTheDocument();
    expect(screen.getByText(/Un traitement est déjà en cours/)).toBeInTheDocument();
  });

  it("un refus métier reste une erreur lisible", async () => {
    rawResponder = () => ({ status: 409, body: { detail: "no_eligible_documents" } });
    await selectAndOpenModal();
    fireEvent.click(screen.getByRole("button", { name: "Lancer la reprise" }));
    expect(await screen.findByText(/Aucun document reprenable/)).toBeInTheDocument();
  });
});

// --- Suivi et résultat ------------------------------------------------------

describe("Suivi du job", () => {
  it("reprend un job actif au chargement de la page", async () => {
    jobResponder = (url: string) =>
      url.includes("active-reindex-job") ? { active: true, job: job() } : job();
    render(<DocumentsPage />);
    await waitForListCalls(1);
    expect(await screen.findByText("Reprise des documents en erreur")).toBeInTheDocument();
  });

  it("le résumé final et la relance des échecs sont proposés", async () => {
    jobResponder = (url: string) =>
      url.includes("active-reindex-job")
        ? {
            active: true,
            job: job({ status: "completed_with_errors", succeeded: 172, failed: 12, running: 0, pending: 0, progress_percent: 100 }),
          }
        : job();
    render(<DocumentsPage />);
    await waitForListCalls(1);
    expect(await screen.findByText(/172 documents prêts · 12 toujours en erreur/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Relancer les 12 échecs/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Fermer" })).toBeInTheDocument();
  });

  it("« Relancer les échecs » cible le job précédent", async () => {
    jobResponder = (url: string) =>
      url.includes("active-reindex-job")
        ? { active: true, job: job({ status: "completed_with_errors", failed: 12, running: 0, pending: 0 }) }
        : job();
    rawResponder = () => ({
      status: 202,
      body: { status: "accepted", job_id: "JOB2", already_running: false },
    });
    render(<DocumentsPage />);
    await waitForListCalls(1);
    fireEvent.click(await screen.findByRole("button", { name: /Relancer les 12 échecs/ }));
    await waitFor(() => expect(postCalls).toHaveLength(1));
    expect(lastPost().url).toBe("/api/v1/reindex-jobs/JOB1/retry-failed/");
    // Sans stratégie : la méthode du job parent est CONSERVÉE.
    expect(lastPost().body).toBeNull();
  });

  it("un job actif verrouille le bouton de réindexation globale", async () => {
    jobResponder = (url: string) =>
      url.includes("active-reindex-job") ? { active: true, job: job() } : job();
    render(<DocumentsPage />);
    await waitForListCalls(1);
    await screen.findByText("Reprise des documents en erreur");
    expect(screen.getByRole("button", { name: /Réindexation/ })).toBeDisabled();
  });
});

// --- Bouton global Réindexer (contrat corrigé) ------------------------------

describe("Bouton global Réindexer", () => {
  it("un 202 ouvre le suivi et n'annonce JAMAIS « 0 réussis, 0 en échec »", async () => {
    rawResponder = () => ({
      status: 202,
      body: { status: "accepted", job_id: "JOB1", kind: "full_reindex", total_documents: 237, already_running: false },
    });
    jobResponder = (url: string) =>
      url.includes("active-reindex-job")
        ? { active: false, job: null }
        : job({ kind: "full_reindex", succeeded: 0, failed: 0, status: "pending", progress_percent: 0 });

    render(<DocumentsPage />);
    await waitForListCalls(1);
    fireEvent.click(await screen.findByRole("button", { name: "Réindexer" }));
    fireEvent.click(await screen.findByRole("button", { name: "Réindexer maintenant" }));

    await waitFor(() => expect(postCalls).toHaveLength(1));
    expect(lastPost().url).toBe("/api/v1/rag-spaces/SP1/reindex/");
    expect(await screen.findByText("Réindexation de l'espace")).toBeInTheDocument();
    expect(screen.getByText(/Réindexation lancée : 237 documents en file/)).toBeInTheDocument();
    // Le défaut hérité du lot 1 : l'ancien contrat produisait ce message au démarrage.
    expect(screen.queryByText(/Réindexation terminée/)).not.toBeInTheDocument();
    expect(screen.queryByText(/0 réussis/)).not.toBeInTheDocument();
  });

  it("un 409 est présenté comme un job à suivre, pas comme une erreur", async () => {
    rawResponder = () => ({
      status: 409,
      body: {
        status: "already_running",
        job_id: "JOB-X",
        already_running: true,
        job: job({ job_id: "JOB-X", kind: "full_reindex" }),
      },
    });
    render(<DocumentsPage />);
    await waitForListCalls(1);
    fireEvent.click(await screen.findByRole("button", { name: "Réindexer" }));
    fireEvent.click(await screen.findByRole("button", { name: "Réindexer maintenant" }));
    expect(await screen.findByText(/Un traitement est déjà en cours/)).toBeInTheDocument();
    expect(screen.getByText("Réindexation de l'espace")).toBeInTheDocument();
  });
});
