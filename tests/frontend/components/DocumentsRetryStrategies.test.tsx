/**
 * Stratégies de reprise — ce que l'écran propose, et surtout ce qu'il NE propose pas.
 *
 * Le défaut corrigé par le lot 3 : l'écran affichait « Réindexer avec OCR » sur des
 * documents dont l'OCR venait précisément de ne rien produire. La règle vérifiée
 * ici est donc qu'aucune action n'est proposée sans que le backend l'ait jugée
 * possible — le frontend relaie une décision, il n'en prend aucune.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const listCalls: string[] = [];
const postCalls: Array<{ url: string; body: any }> = [];
let listResponder: (url: string) => any;
let previewResponder: (body: any) => any;
let rawResponder: (url: string, init: any) => { status: number; body: any };
let jobResponder: (url: string) => any;

const apiFetch = vi.fn(async (url: string, init: any = {}) => {
  if (url.startsWith("/api/v1/documents/?")) {
    listCalls.push(url);
    return listResponder(url);
  }
  if (url.includes("/retry-failed-documents/preview/")) {
    const body = init?.body ? JSON.parse(init.body) : null;
    postCalls.push({ url, body });
    return previewResponder(body);
  }
  if (url.includes("/reindex/") || url.includes("/reindex-jobs/") || url.includes("/active-reindex-job/")) {
    if (init?.method === "POST") postCalls.push({ url, body: init?.body ? JSON.parse(init.body) : null });
    return jobResponder(url);
  }
  return {};
});

async function defaultRawImpl(url: string, init: any = {}) {
  postCalls.push({ url, body: init?.body ? JSON.parse(init.body) : null });
  const { status, body } = rawResponder(url, init);
  return { status, json: async () => body } as any;
}
const apiFetchRaw = vi.fn(defaultRawImpl);

vi.mock("@/lib/api", () => ({
  apiFetch: (...a: any[]) => apiFetch(...(a as [string, any])),
  apiFetchRaw: (...a: any[]) => apiFetchRaw(...(a as [string, any])),
  apiUploadForm: vi.fn(),
  apiFetchBlobUrl: vi.fn(),
  deleteDocument: vi.fn(),
  getCapabilities: vi.fn().mockResolvedValue({ embeddings: { provider: "ollama" } }),
  getWorkspaceQuota: vi.fn().mockResolvedValue({
    max_document_count: 5000, used_document_count: 10,
    max_document_bytes: 1024 ** 4, used_document_bytes: 1024,
    count_usage_percent: 1, size_usage_percent: 1, is_exceeded: false, can_upload: true,
  }),
  summarizeDocumentAwait: vi.fn(),
  summaryErrorKey: vi.fn(),
}));

vi.mock("@/components/shell", () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Topbar: () => null,
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

import { __resetUploadScheduler } from "@/app/documents/_lib/uploadScheduler";
import DocumentsPage from "@/app/documents/page";

// --- Données ----------------------------------------------------------------

/** Document en échec, avec la classification telle que le BACKEND la renvoie. */
function failedDoc(id: string, name: string, extraction: any) {
  return {
    id, rag_space_id: "SP1", filename: name, content_type: "application/pdf",
    file_type: "PDF", size_bytes: 100, sha256: "x", status: "FAILED",
    error_message: `${extraction.error_code}: …`, created_at: "2026-07-28T14:37:00Z",
    chunks_count: 0, extraction,
  };
}

const DOCS = {
  scan: failedDoc("D-SCAN", "scan.pdf", {
    status: "ocr_required", error_code: "pdf_scanned_requires_ocr",
    retryability: "retryable_with_strategy", recommended_strategy: "force_ocr",
  }),
  slow: failedDoc("D-SLOW", "lourd.pdf", {
    status: "rejected", error_code: "extraction_timeout",
    retryability: "retryable_with_strategy", recommended_strategy: "extended_timeout",
  }),
  net: failedDoc("D-NET", "reseau.pdf", {
    status: "failed", error_code: "transient_ingest_error",
    retryability: "retryable", recommended_strategy: "standard",
  }),
  noText: failedDoc("D-NOTEXT", "illisible.pdf", {
    status: "ocr_required", error_code: "ocr_no_text",
    retryability: "replace_source_recommended", recommended_strategy: null,
  }),
  dead: failedDoc("D-DEAD", "macro.docx", {
    status: "rejected", error_code: "macro_enabled_document_refused",
    retryability: "non_retryable", recommended_strategy: null,
  }),
};

const COUNTS = { all: 5, ready: 0, analyzing: 0, queued: 0, error: 5 };

function listWith(items: any[]) {
  return (url: string) => ({
    items, total: items.length, limit: 50, offset: 0,
    has_next: false, has_previous: false,
    ordering: new URLSearchParams(url.split("?")[1]).get("ordering"),
    counts: { ...COUNTS, all: items.length, error: items.length },
  });
}

const lastPost = () => postCalls[postCalls.length - 1];
const postsTo = (fragment: string) => postCalls.filter((c) => c.url.includes(fragment));

async function waitForList(n = 1) {
  await waitFor(() => expect(listCalls.length).toBeGreaterThanOrEqual(n), { timeout: 3000 });
}

/** Ouvre l'onglet Erreurs (les cases et les actions y sont réservées). */
async function openErrors() {
  render(<DocumentsPage />);
  await waitForList(1);
  fireEvent.click(await screen.findByRole("tab", { name: /Erreurs/ }));
  await waitFor(() =>
    expect(new URLSearchParams(listCalls[listCalls.length - 1].split("?")[1]).get("status")).toBe("error")
  );
}

/** Ouvre le menu ⋯ d'une ligne et renvoie ses entrées. */
async function openRowMenu(index = 0) {
  const buttons = await screen.findAllByRole("button", { name: "Actions du document" });
  fireEvent.click(buttons[index]);
  return await screen.findAllByRole("menuitem");
}

beforeEach(() => {
  // La cadence d'envoi est PARTAGÉE au niveau du module (un onglet = une
  // cadence). Sans remise à zéro, le créneau réservé par un test repousse le
  // premier envoi du suivant et le fait expirer.
  __resetUploadScheduler();
  listCalls.length = 0;
  postCalls.length = 0;
  listResponder = listWith(Object.values(DOCS));
  previewResponder = () => ({
    requested_strategy: "auto",
    total_documents: 5,
    actionable_documents: 3,
    skipped_documents: 2,
    strategy_breakdown: { force_ocr: 1, extended_timeout: 1, standard: 1 },
    skip_reasons: { non_retryable: 1, replace_source_recommended: 1 },
    extended_timeout_seconds: 480,
  });
  rawResponder = () => ({
    status: 202,
    body: { status: "accepted", job_id: "JOB1", requested_strategy: "auto",
            total_documents: 3, already_running: false },
  });
  jobResponder = (url: string) =>
    url.includes("active-reindex-job") ? { active: false, job: null } : { ok: true };
  apiFetch.mockClear();
  apiFetchRaw.mockReset();
  apiFetchRaw.mockImplementation(defaultRawImpl);
});

// --- Menu unitaire ----------------------------------------------------------

describe("Action recommandée par document", () => {
  it("un PDF scanné propose l'OCR en premier, marqué recommandé", async () => {
    listResponder = listWith([DOCS.scan]);
    await openErrors();
    const items = await openRowMenu();
    const labels = items.map((i) => i.textContent);
    const retryLabels = labels.filter((l) => /Réessayer/.test(l || ""));
    // La méthode recommandée arrive EN PREMIER parmi les reprises proposées.
    expect(retryLabels[0]).toMatch(/Réessayer — Forcer l'OCR \(recommandé\)/);
    expect(retryLabels).toContain("Réessayer normalement");
  });

  it("un dépassement de délai propose le délai étendu", async () => {
    listResponder = listWith([DOCS.slow]);
    await openErrors();
    const labels = (await openRowMenu()).map((i) => i.textContent);
    expect(labels.some((l) => /Réessayer — Délai étendu \(recommandé\)/.test(l || ""))).toBe(true);
    expect(labels.some((l) => /OCR/.test(l || ""))).toBe(false);
  });

  it("une erreur transitoire propose une reprise standard", async () => {
    listResponder = listWith([DOCS.net]);
    await openErrors();
    const labels = (await openRowMenu()).map((i) => i.textContent);
    expect(labels.some((l) => /Réessayer — Standard \(recommandé\)/.test(l || ""))).toBe(true);
    expect(labels.some((l) => /OCR/.test(l || ""))).toBe(false);
  });

  it("après un OCR sans texte, AUCUNE reprise OCR n'est proposée", async () => {
    // Le défaut historique : reproposer exactement ce qui vient d'échouer.
    listResponder = listWith([DOCS.noText]);
    await openErrors();
    const labels = (await openRowMenu()).map((i) => i.textContent);
    expect(labels.some((l) => /OCR/.test(l || ""))).toBe(false);
    expect(labels.some((l) => /Réessayer/.test(l || ""))).toBe(false);
    expect(screen.getByRole("note")).toHaveTextContent(/Remplacez le fichier source/);
  });

  it("un échec définitif l'annonce sans proposer d'action", async () => {
    listResponder = listWith([DOCS.dead]);
    await openErrors();
    const labels = (await openRowMenu()).map((i) => i.textContent);
    expect(labels.some((l) => /Réessayer/.test(l || ""))).toBe(false);
    expect(screen.getByRole("note")).toHaveTextContent(/ne peut pas être réingéré automatiquement/);
  });

  it("le clic envoie la stratégie choisie, jamais ?ocr=true", async () => {
    listResponder = listWith([DOCS.scan]);
    await openErrors();
    const items = await openRowMenu();
    fireEvent.click(items.find((i) => /Forcer l'OCR/.test(i.textContent || ""))!);
    await waitFor(() => expect(postsTo("/reindex/")).toHaveLength(1));
    const call = postsTo("/reindex/")[0];
    expect(call.url).toBe("/api/v1/documents/D-SCAN/reindex/");
    expect(call.body).toEqual({ strategy: "force_ocr" });
    expect(call.url).not.toContain("ocr=true");
  });

  it("« Réessayer normalement » envoie la stratégie standard", async () => {
    listResponder = listWith([DOCS.scan]);
    await openErrors();
    const items = await openRowMenu();
    fireEvent.click(items.find((i) => i.textContent === "Réessayer normalement")!);
    await waitFor(() => expect(postsTo("/reindex/")).toHaveLength(1));
    expect(postsTo("/reindex/")[0].body).toEqual({ strategy: "standard" });
  });
});

// --- Modale de masse --------------------------------------------------------

describe("Choix de la méthode pour une reprise en masse", () => {
  async function openModal() {
    await openErrors();
    fireEvent.click(await screen.findByRole("checkbox", { name: /Sélectionner les documents de cette page/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Réingérer la sélection" }));
    await screen.findByRole("button", { name: "Lancer la reprise" });
  }

  it("« Automatique » est sélectionné par défaut", async () => {
    await openModal();
    expect(screen.getByRole("radio", { name: /Automatique — recommandé/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /^Standard/ })).not.toBeChecked();
  });

  it("les quatre méthodes sont proposées", async () => {
    await openModal();
    for (const label of [/Automatique/, /^Standard/, /Forcer l'OCR/, /Délai étendu/]) {
      expect(screen.getByRole("radio", { name: label })).toBeInTheDocument();
    }
  });

  it("la prévisualisation détaille la répartition avant lancement", async () => {
    await openModal();
    const preview = await screen.findByTestId("retry-preview");
    expect(preview).toHaveTextContent("1 document : Forcer l'OCR");
    expect(preview).toHaveTextContent("1 document : Délai étendu");
    expect(preview).toHaveTextContent("1 document : Standard");
    expect(preview).toHaveTextContent("2 documents sans reprise automatique pertinente");
    expect(preview).toHaveTextContent("480 s par document");
  });

  it("la prévisualisation ne crée aucun job", async () => {
    await openModal();
    await screen.findByTestId("retry-preview");
    expect(postsTo("/retry-failed-documents/preview/")).toHaveLength(1);
    expect(postsTo("/retry-failed-documents/").filter((c) => !c.url.includes("preview"))).toHaveLength(0);
  });

  it("changer de méthode rejoue la prévisualisation", async () => {
    await openModal();
    await screen.findByTestId("retry-preview");
    previewResponder = () => ({
      requested_strategy: "force_ocr", total_documents: 5, actionable_documents: 2,
      skipped_documents: 3, strategy_breakdown: { force_ocr: 2 },
      skip_reasons: { strategy_not_supported: 1, non_retryable: 2 },
      extended_timeout_seconds: null,
    });
    fireEvent.click(screen.getByRole("radio", { name: /Forcer l'OCR/ }));
    await waitFor(() =>
      expect(lastPost().body.strategy).toBe("force_ocr")
    );
    await waitFor(() =>
      expect(screen.getByTestId("retry-preview")).toHaveTextContent("2 documents : Forcer l'OCR")
    );
  });

  it("la méthode standard est avertie quand elle risque de rejouer l'erreur", async () => {
    await openModal();
    fireEvent.click(screen.getByRole("radio", { name: /^Standard/ }));
    await waitFor(() =>
      expect(screen.getByTestId("retry-preview")).toHaveTextContent(
        /reproduira probablement l'erreur précédente/
      )
    );
  });

  it("le lancement transmet la méthode choisie", async () => {
    await openModal();
    fireEvent.click(screen.getByRole("radio", { name: /Délai étendu/ }));
    await waitFor(() => expect(lastPost().body.strategy).toBe("extended_timeout"));
    fireEvent.click(screen.getByRole("button", { name: "Lancer la reprise" }));
    await waitFor(() =>
      expect(postsTo("/retry-failed-documents/").filter((c) => !c.url.includes("preview"))).toHaveLength(1)
    );
    const call = postsTo("/retry-failed-documents/").filter((c) => !c.url.includes("preview"))[0];
    expect(call.body.strategy).toBe("extended_timeout");
    expect(call.body.selection.mode).toBe("explicit");
  });

  it("un double clic ne lance qu'un seul job", async () => {
    let release: (v: any) => void = () => {};
    apiFetchRaw.mockImplementation(async (url: string, init: any = {}) => {
      postCalls.push({ url, body: init?.body ? JSON.parse(init.body) : null });
      return new Promise((resolve) => {
        release = () => resolve({
          status: 202,
          json: async () => ({ status: "accepted", job_id: "JOB1", already_running: false }),
        } as any);
      });
    });
    await openModal();
    const btn = screen.getByRole("button", { name: "Lancer la reprise" });
    fireEvent.click(btn);
    fireEvent.click(btn);
    await waitFor(() => expect(postsTo("/retry-failed-documents/").filter((c) => !c.url.includes("preview"))).toHaveLength(1));
    release(null);
  });

  it("le sélecteur de méthode est utilisable au clavier", async () => {
    await openModal();
    const std = screen.getByRole("radio", { name: /^Standard/ });
    std.focus();
    await userEvent.keyboard(" ");
    expect(std).toBeChecked();
  });

  it("un lot sans document reprenable est dit clairement", async () => {
    rawResponder = () => ({ status: 409, body: { detail: "no_retryable_documents" } });
    await openModal();
    fireEvent.click(screen.getByRole("button", { name: "Lancer la reprise" }));
    expect(await screen.findByText(/Aucun document reprenable/)).toBeInTheDocument();
  });
});

// --- Suivi enrichi ----------------------------------------------------------

describe("Suivi d'un job par stratégie", () => {
  const runningJob = {
    job_id: "JOB1", rag_space_id: "SP1", status: "running",
    kind: "failed_documents_retry", requested_strategy: "auto",
    strategy_breakdown: {
      force_ocr: { total: 38, succeeded: 30, failed: 8 },
      extended_timeout: { total: 20, succeeded: 18, failed: 2 },
    },
    skipped: 6, skip_reasons: { non_retryable: 4, replace_source_recommended: 2 },
    total: 64, pending: 0, queued: 0, running: 6, succeeded: 48, failed: 10,
    cancelled: 0, interrupted: 0, progress_percent: 90,
    parent_job_id: null, error_code: null,
    started_at: null, finished_at: null, created_at: null, updated_at: null,
  };

  it("affiche l'avancement par méthode", async () => {
    jobResponder = (url: string) =>
      url.includes("active-reindex-job") ? { active: true, job: runningJob } : runningJob;
    render(<DocumentsPage />);
    await waitForList(1);
    expect(await screen.findByTestId("job-strategy-force_ocr")).toHaveTextContent(
      "Forcer l'OCR : 30/38 · 8 en échec"
    );
    expect(screen.getByTestId("job-strategy-extended_timeout")).toHaveTextContent(
      "Délai étendu : 18/20 · 2 en échec"
    );
  });

  it("affiche les raisons structurées des documents non traités", async () => {
    jobResponder = (url: string) =>
      url.includes("active-reindex-job") ? { active: true, job: runningJob } : runningJob;
    render(<DocumentsPage />);
    await waitForList(1);
    expect(await screen.findByText(/6 documents non traités/)).toBeInTheDocument();
    expect(screen.getByTestId("job-skip-non_retryable")).toHaveTextContent("4 définitifs");
    expect(screen.getByTestId("job-skip-replace_source_recommended")).toHaveTextContent(
      "2 nécessitant un autre fichier source"
    );
  });

  it("propose de relancer à l'identique OU automatiquement", async () => {
    const terminal = { ...runningJob, status: "completed_with_errors", running: 0, failed: 10 };
    jobResponder = (url: string) =>
      url.includes("active-reindex-job") ? { active: true, job: terminal } : terminal;
    rawResponder = () => ({ status: 202, body: { status: "accepted", job_id: "JOB2", already_running: false } });
    render(<DocumentsPage />);
    await waitForList(1);

    // Relance à l'identique : aucun corps, la méthode du parent est conservée.
    fireEvent.click(await screen.findByRole("button", { name: /Relancer les 10 échecs/ }));
    await waitFor(() => expect(postsTo("/retry-failed/")).toHaveLength(1));
    expect(postsTo("/retry-failed/")[0].body).toBeNull();

    // Relance automatique : recalcul sur les causes les plus récentes.
    fireEvent.click(screen.getByRole("button", { name: /Réessayer automatiquement les 10 échecs/ }));
    await waitFor(() => expect(postsTo("/retry-failed/")).toHaveLength(2));
    expect(postsTo("/retry-failed/")[1].body).toEqual({ strategy: "auto" });
  });

  it("un job global n'affiche pas de répartition par méthode", async () => {
    const global = { ...runningJob, kind: "full_reindex", requested_strategy: "standard",
                     strategy_breakdown: { standard: { total: 64, succeeded: 48, failed: 10 } },
                     skipped: 0, skip_reasons: {} };
    jobResponder = (url: string) =>
      url.includes("active-reindex-job") ? { active: true, job: global } : global;
    render(<DocumentsPage />);
    await waitForList(1);
    await screen.findByText("Réindexation de l'espace");
    // Une seule méthode : la ventilation n'apprendrait rien.
    expect(screen.queryByTestId("job-strategy-standard")).not.toBeInTheDocument();
    expect(screen.queryByText(/documents non traités/)).not.toBeInTheDocument();
  });
});
