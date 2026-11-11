/**
 * Formulaire de configuration IA d'un Space — alignement sur le catalogue gouverné.
 *
 * L'écran précédent posait le même mot, « Provider », sur deux sections qui ne
 * commandaient pas la même chose : une préférence LOGIQUE pour le chat (qui passe par
 * la passerelle quel que soit le choix) et un TRANSPORT réel pour les embeddings. Et
 * ses deux champs de modèle étaient du texte libre : `bge-m3` y était accepté, marquait
 * tous les documents à réindexer, puis échouait à l'ingestion.
 *
 * Ces tests vérifient que l'écran dit désormais la vérité du runtime, et qu'il ne peut
 * plus produire une configuration que le backend vient précisément d'interdire.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";

const apiFetch = vi.fn();
vi.mock("@/lib/api", () => ({ apiFetch: (...a: any[]) => apiFetch(...a) }));

const wsState = vi.hoisted(() => ({ isSuperuser: true }));
vi.mock("@/components/shell", () => ({
  useWorkspace: () => ({ isSuperuser: wsState.isSuperuser }),
}));

import { SpaceEditModal } from "@/app/workspace-admin/_components/SpaceEditModal";

const SPACE = { id: "s1", name: "RH" };

/** Catalogue FIDÈLE à celui projeté par le backend en staging. */
function catalog(over: Partial<any> = {}) {
  return {
    chat: [
      { id: "qwen3:30b-instruct", kind: "chat", display_name: "qwen-large", available: true,
        allowed: true, qualified: true, recommended: true,
        capabilities: ["completion", "tools"], registry_status: "",
        upstream_model: "qwen3:30b-instruct" },
      { id: "llama3.1:8b", kind: "chat", display_name: "llama-chat", available: true,
        allowed: true, qualified: false, recommended: false,
        capabilities: ["completion", "tools"], registry_status: "",
        upstream_model: "llama3.1:8b" },
    ],
    embeddings: [
      { id: "bge-m3:latest", kind: "embedding", display_name: "bge-m3:latest", available: true,
        allowed: false, qualified: false, recommended: false, capabilities: ["embedding"],
        registry_status: "", upstream_model: "bge-m3:latest", dimension: 1024,
        reason: "model_registry_not_ready" },
      { id: "fr-en-retrieval", kind: "embedding", display_name: "fr-en-retrieval", available: true,
        allowed: true, qualified: true, recommended: false, capabilities: ["embedding"],
        registry_status: "ready", upstream_model: "granite-embedding:278m", dimension: 768 },
      { id: "legacy-768", kind: "embedding", display_name: "legacy-768", available: true,
        allowed: true, qualified: true, recommended: true, capabilities: ["embedding"],
        registry_status: "ready", upstream_model: "nomic-embed-text:latest", dimension: 768 },
    ],
    available: true, stale: false,
    qualification_revision: "2026-08-22.1",
    expected_embedding_dimension: 768,
    ...over,
  };
}

function settings(over: Partial<any> = {}) {
  return {
    llm_provider: "ollama",
    llm_model: "qwen3:30b-instruct",
    embedding_provider: "ai_gateway",
    embedding_model: "fr-en-retrieval",
    can_edit_model_configuration: true,
    documents: { total: 24, ready: 24, failed: 0, reindex_required: 0 },
    ...over,
  };
}

/** Route les appels selon l'URL — l'ordre des requêtes n'est pas un contrat. */
function route(map: Record<string, any>) {
  apiFetch.mockImplementation((url: string) => {
    for (const [frag, value] of Object.entries(map)) {
      if (url.includes(frag)) {
        return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
      }
    }
    return Promise.reject(new Error(`URL non routée: ${url}`));
  });
}

const noop = async () => {};

function renderEdit(over: Partial<any> = {}, cat: any = catalog()) {
  route({ "model-catalog": cat, "llm-settings": settings(over) });
  return render(
    <SpaceEditModal space={SPACE as any} onClose={noop} onCreate={noop} onPatch={noop} />
  );
}

function pipelineSelect() {
  return screen.getByLabelText("Pipeline d'embeddings") as HTMLSelectElement;
}
function chatSelect() {
  return screen.getByLabelText("Modèle de génération") as HTMLSelectElement;
}

beforeEach(() => {
  apiFetch.mockReset();
  wsState.isSuperuser = true;
});

// ---------------------------------------------------------------------------
// Catalogue — §46
// ---------------------------------------------------------------------------

describe("catalogue", () => {
  it("charge le catalogue et alimente les deux sélecteurs", async () => {
    renderEdit();
    await waitFor(() => expect(chatSelect()).toBeTruthy());
    expect(apiFetch.mock.calls.some((c) => String(c[0]).includes("/api/v1/ai/model-catalog/"))).toBe(true);
    const chatValues = [...chatSelect().options].map((o) => o.value);
    expect(chatValues).toContain("qwen3:30b-instruct");
    expect(chatValues).toContain("llama3.1:8b");
  });

  it("le navigateur n'appelle jamais la passerelle directement", async () => {
    renderEdit();
    await waitFor(() => expect(chatSelect()).toBeTruthy());
    const urls = apiFetch.mock.calls.map((c) => String(c[0]));
    expect(urls.every((u) => u.startsWith("/api/"))).toBe(true);
    expect(urls.some((u) => u.includes("/v1/models"))).toBe(false);
  });

  it("un modèle NON autorisé n'est pas sélectionnable", async () => {
    renderEdit();
    await waitFor(() => expect(pipelineSelect()).toBeTruthy());
    const values = [...pipelineSelect().options].map((o) => o.value);
    expect(values).not.toContain("bge-m3:latest");
    expect(values).toContain("legacy-768");
    expect(values).toContain("fr-en-retrieval");
  });

  it("aucun champ de saisie libre de modèle ne subsiste", async () => {
    const { container } = renderEdit();
    await waitFor(() => expect(pipelineSelect()).toBeTruthy());
    const texts = [...container.querySelectorAll('input[type="text"], input:not([type])')];
    // Seul le NOM de l'espace reste un champ libre.
    expect(texts.length).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// Sémantique chat — §5-9
// ---------------------------------------------------------------------------

describe("chat", () => {
  it("parle de « Moteur » et non de « Provider »", async () => {
    renderEdit();
    await waitFor(() => expect(screen.getByLabelText("Moteur")).toBeTruthy());
    const group = screen.getByLabelText("Moteur");
    expect(within(group).getByText("Local")).toBeTruthy();
    expect(screen.queryByText("Ollama")).toBeNull();
  });

  it("affiche le routage Gateway comme information non éditable", async () => {
    renderEdit();
    await waitFor(() => expect(screen.getByText(/Routage : AI Gateway/)).toBeTruthy());
    // Aucune IP ni port exposés à l'utilisateur.
    expect(document.body.textContent).not.toMatch(/192\.168|:8100|:8200/);
  });

  it("marque le modèle recommandé et distingue les non qualifiés", async () => {
    renderEdit();
    await waitFor(() => expect(chatSelect()).toBeTruthy());
    expect(screen.getAllByText("Recommandé").length).toBeGreaterThan(0);
    fireEvent.change(chatSelect(), { target: { value: "llama3.1:8b" } });
    await waitFor(() => expect(screen.getByText("Non qualifié")).toBeTruthy());
  });

  it("un modèle autorisé mais non qualifié reste sélectionnable", async () => {
    renderEdit();
    await waitFor(() => expect(chatSelect()).toBeTruthy());
    fireEvent.change(chatSelect(), { target: { value: "llama3.1:8b" } });
    expect(chatSelect().value).toBe("llama3.1:8b");
  });
});

// ---------------------------------------------------------------------------
// Pipelines d'embeddings — §10-14, §29
// ---------------------------------------------------------------------------

describe("pipeline d'embeddings", () => {
  it("le choix principal est le pipeline, pas le provider technique", async () => {
    renderEdit();
    await waitFor(() => expect(pipelineSelect()).toBeTruthy());
    expect(screen.queryByLabelText("Fournisseur")).toBeNull();
  });

  it("legacy-768 est proposé comme recommandé, fr-en-retrieval reste visible", async () => {
    renderEdit();
    await waitFor(() => expect(pipelineSelect()).toBeTruthy());
    const labels = [...pipelineSelect().options].map((o) => o.textContent || "");
    expect(labels.some((l) => l.includes("legacy-768") && l.includes("Recommandé"))).toBe(true);
    expect(labels.some((l) => l.includes("fr-en-retrieval") && l.includes("Qualifié"))).toBe(true);
  });

  it("affiche la dimension et le modèle effectif du pipeline choisi", async () => {
    renderEdit();
    await waitFor(() => expect(pipelineSelect()).toBeTruthy());
    expect(screen.getByText(/Dimension/)).toBeTruthy();
    expect(screen.getByText(/granite-embedding:278m/)).toBeTruthy();
  });

  it("n'expose pas le digest comme choix", async () => {
    renderEdit();
    await waitFor(() => expect(pipelineSelect()).toBeTruthy());
    expect(document.body.textContent).not.toMatch(/1a37926b/);
  });

  it("ne promet pas « meilleure qualité » pour fr-en-retrieval", async () => {
    renderEdit();
    await waitFor(() => expect(pipelineSelect()).toBeTruthy());
    expect(document.body.textContent).not.toMatch(/meilleure qualité/i);
    expect(screen.getByText("Latence plus élevée")).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// Espaces historiques — §39-41, §51
// ---------------------------------------------------------------------------

describe("espace historique", () => {
  const legacy = {
    embedding_provider: "ollama",
    embedding_model: "nomic-embed-text",
    documents: { total: 10, ready: 8, failed: 2, reindex_required: 0 },
  };

  it("est affiché comme Legacy, pas comme legacy-768", async () => {
    renderEdit(legacy);
    await waitFor(() => expect(pipelineSelect()).toBeTruthy());
    expect(pipelineSelect().value).toBe("nomic-embed-text");
    const selected = pipelineSelect().selectedOptions[0].textContent || "";
    expect(selected).toMatch(/Legacy/);
    expect(selected).not.toMatch(/legacy-768/);
  });

  it("n'est pas migré implicitement à l'ouverture du formulaire", async () => {
    const onPatch = vi.fn().mockResolvedValue(undefined);
    route({ "model-catalog": catalog(), "llm-settings": settings(legacy) });
    render(<SpaceEditModal space={SPACE as any} onClose={noop} onCreate={noop} onPatch={onPatch} />);
    await waitFor(() => expect(pipelineSelect()).toBeTruthy());
    fireEvent.click(screen.getByText("Enregistrer"));
    await waitFor(() => expect(onPatch).toHaveBeenCalled());
    const payload = onPatch.mock.calls[0][1];
    expect(payload.embedding_provider).toBe("ollama");
    expect(payload.embedding_model).toBe("nomic-embed-text");
  });

  it("propose la migration vers un pipeline gouverné sans l'imposer", async () => {
    renderEdit(legacy);
    await waitFor(() => expect(pipelineSelect()).toBeTruthy());
    const values = [...pipelineSelect().options].map((o) => o.value);
    expect(values).toContain("nomic-embed-text");
    expect(values).toContain("legacy-768");
  });
});

// ---------------------------------------------------------------------------
// Avertissement de réindexation — §14, §31-34
// ---------------------------------------------------------------------------

describe("avertissement de réindexation", () => {
  it("chiffre les documents prêts et distingue ceux en erreur", async () => {
    renderEdit({
      embedding_model: "legacy-768",
      documents: { total: 288, ready: 242, failed: 44, reindex_required: 46 },
    });
    await waitFor(() => expect(pipelineSelect()).toBeTruthy());
    fireEvent.change(pipelineSelect(), { target: { value: "fr-en-retrieval" } });
    const warning = await screen.findByTestId("reindex-warning");
    expect(warning.textContent).toMatch(/242 documents prêts seront marqués/);
    expect(warning.textContent).toMatch(/44 documents en erreur ne seront pas réparés/);
    // Le total trompeur ne doit apparaître nulle part.
    expect(warning.textContent).not.toMatch(/286 documents/);
  });

  it("rappelle que la réindexation n'est pas automatique", async () => {
    renderEdit({ embedding_model: "legacy-768" });
    await waitFor(() => expect(pipelineSelect()).toBeTruthy());
    fireEvent.change(pipelineSelect(), { target: { value: "fr-en-retrieval" } });
    const warning = await screen.findByTestId("reindex-warning");
    expect(warning.textContent).toMatch(/n'est pas lancée automatiquement/);
  });

  it("ne chiffre rien tant que le pipeline n'a pas changé", async () => {
    renderEdit();
    const warning = await screen.findByTestId("reindex-warning");
    expect(warning.textContent).not.toMatch(/seront marqués/);
  });
});

// ---------------------------------------------------------------------------
// Catalogue indisponible — §25
// ---------------------------------------------------------------------------

describe("catalogue indisponible", () => {
  const down = { chat: [], embeddings: [], available: false, stale: false,
                 error: "model_catalog_unavailable", qualification_revision: "",
                 expected_embedding_dimension: 768 };

  it("désactive la configuration sans réactiver un champ libre", async () => {
    const { container } = renderEdit({}, down);
    await waitFor(() => expect(screen.getByTestId("catalog-unavailable")).toBeTruthy());
    expect(screen.queryByLabelText("Modèle de génération")).toBeNull();
    expect(screen.queryByLabelText("Pipeline d'embeddings")).toBeNull();
    expect([...container.querySelectorAll('input[type="text"], input:not([type])')].length).toBe(1);
  });

  it("affiche tout de même la configuration actuelle", async () => {
    renderEdit({}, down);
    await waitFor(() => expect(screen.getByTestId("catalog-unavailable")).toBeTruthy());
    expect(screen.getByText("fr-en-retrieval")).toBeTruthy();
  });

  it("ne soumet pas les champs modèle", async () => {
    const onPatch = vi.fn().mockResolvedValue(undefined);
    route({ "model-catalog": down, "llm-settings": settings() });
    render(<SpaceEditModal space={SPACE as any} onClose={noop} onCreate={noop} onPatch={onPatch} />);
    await waitFor(() => expect(screen.getByTestId("catalog-unavailable")).toBeTruthy());
    fireEvent.click(screen.getByText("Enregistrer"));
    await waitFor(() => expect(onPatch).toHaveBeenCalled());
    expect(Object.keys(onPatch.mock.calls[0][1])).toEqual(["name"]);
  });
});

// ---------------------------------------------------------------------------
// Permissions — §18-20
// ---------------------------------------------------------------------------

describe("permissions", () => {
  it("un non-superuser voit la configuration en lecture seule", async () => {
    wsState.isSuperuser = false;
    renderEdit();
    await waitFor(() =>
      expect(screen.getByText(/définie par l'administrateur plateforme/)).toBeTruthy()
    );
    expect(screen.queryByLabelText("Modèle de génération")).toBeNull();
    expect(screen.queryByLabelText("Pipeline d'embeddings")).toBeNull();
    expect(screen.getByText("fr-en-retrieval")).toBeTruthy();
  });

  it("un 403 sur l'espace affiche un message, pas un formulaire vide éditable", async () => {
    // Sous enforcement owner-scope, un superuser peut être autorisé à changer des
    // modèles en général et ne pas atteindre CET espace (cas QUALIF-SPACE en staging).
    // Sans ce traitement, l'écran présentait des sélecteurs vides et l'utilisateur
    // découvrait le refus après avoir rempli le formulaire.
    const denied: any = new Error("No access to this RAG space");
    denied.status = 403;
    route({ "model-catalog": catalog(), "llm-settings": denied });
    render(<SpaceEditModal space={SPACE as any} onClose={noop} onCreate={noop} onPatch={noop} />);
    await waitFor(() => expect(screen.getByTestId("permission-denied")).toBeTruthy());
    expect(screen.queryByLabelText("Modèle de génération")).toBeNull();
    expect(screen.queryByLabelText("Pipeline d'embeddings")).toBeNull();
  });

  it("un 403 sur l'espace ne soumet que le nom", async () => {
    const denied: any = new Error("No access to this RAG space");
    denied.status = 403;
    const onPatch = vi.fn().mockResolvedValue(undefined);
    route({ "model-catalog": catalog(), "llm-settings": denied });
    render(<SpaceEditModal space={SPACE as any} onClose={noop} onCreate={noop} onPatch={onPatch} />);
    await waitFor(() => expect(screen.getByTestId("permission-denied")).toBeTruthy());
    fireEvent.click(screen.getByText("Enregistrer"));
    await waitFor(() => expect(onPatch).toHaveBeenCalled());
    expect(Object.keys(onPatch.mock.calls[0][1])).toEqual(["name"]);
  });

  it("un non-superuser ne soumet que le nom", async () => {
    wsState.isSuperuser = false;
    const onPatch = vi.fn().mockResolvedValue(undefined);
    route({ "model-catalog": catalog(), "llm-settings": settings() });
    render(<SpaceEditModal space={SPACE as any} onClose={noop} onCreate={noop} onPatch={onPatch} />);
    await waitFor(() => expect(screen.getByText(/administrateur plateforme/)).toBeTruthy());
    fireEvent.click(screen.getByText("Enregistrer"));
    await waitFor(() => expect(onPatch).toHaveBeenCalled());
    expect(Object.keys(onPatch.mock.calls[0][1])).toEqual(["name"]);
  });
});

// ---------------------------------------------------------------------------
// Sauvegarde — §35-37
// ---------------------------------------------------------------------------

describe("sauvegarde", () => {
  it("envoie le pipeline gouverné avec son provider technique", async () => {
    const onPatch = vi.fn().mockResolvedValue(undefined);
    route({ "model-catalog": catalog(), "llm-settings": settings() });
    render(<SpaceEditModal space={SPACE as any} onClose={noop} onCreate={noop} onPatch={onPatch} />);
    await waitFor(() => expect(pipelineSelect()).toBeTruthy());
    fireEvent.change(pipelineSelect(), { target: { value: "legacy-768" } });
    fireEvent.click(screen.getByText("Enregistrer"));
    await waitFor(() => expect(onPatch).toHaveBeenCalled());
    expect(onPatch.mock.calls[0][1]).toMatchObject({
      embedding_provider: "ai_gateway",
      embedding_model: "legacy-768",
      llm_provider: "ollama",
      llm_model: "qwen3:30b-instruct",
    });
  });

  it("un échec API affiche l'erreur et ne ferme pas le modal", async () => {
    const onPatch = vi.fn().mockRejectedValue(new Error("model_dimension_mismatch"));
    const onClose = vi.fn();
    route({ "model-catalog": catalog(), "llm-settings": settings() });
    render(<SpaceEditModal space={SPACE as any} onClose={onClose} onCreate={noop} onPatch={onPatch} />);
    await waitFor(() => expect(pipelineSelect()).toBeTruthy());
    fireEvent.click(screen.getByText("Enregistrer"));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toMatch(/dimension/));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("un échec API restaure la configuration serveur", async () => {
    const onPatch = vi.fn().mockRejectedValue(new Error("refus"));
    route({ "model-catalog": catalog(), "llm-settings": settings() });
    render(<SpaceEditModal space={SPACE as any} onClose={noop} onCreate={noop} onPatch={onPatch} />);
    await waitFor(() => expect(pipelineSelect()).toBeTruthy());
    fireEvent.change(pipelineSelect(), { target: { value: "legacy-768" } });
    fireEvent.click(screen.getByText("Enregistrer"));
    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    // La sélection revient à ce que le serveur porte réellement.
    await waitFor(() => expect(pipelineSelect().value).toBe("fr-en-retrieval"));
  });
});
