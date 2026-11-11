/**
 * Sélecteur de chat filtré sur la capacité RÉELLE de génération (GATE-2).
 *
 * Avant ce lot, `gpt-oss:20b` et `qwen3:30b-a3b-thinking-2507` étaient proposés : la
 * passerelle les refuse sur `/v1/generate`, et l'espace échouait à chaque question. Le
 * un moteur externe était un bouton actif qui aboutissait toujours à un 400 (édition Open : moteur unique).
 *
 * L'écran ne décide rien : il affiche ce que le backend déclare (`allowed`,
 * `generation_status`, `chat_providers`, `default_chat_status`).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within, fireEvent } from "@testing-library/react";

const apiFetch = vi.fn();
vi.mock("@/lib/api", () => ({ apiFetch: (...a: any[]) => apiFetch(...a) }));
vi.mock("@/components/shell", () => ({ useWorkspace: () => ({ isSuperuser: true }) }));

import { SpaceEditModal } from "@/app/workspace-admin/_components/SpaceEditModal";

const chatEntry = (id: string, over: Partial<any> = {}) => ({
  id, kind: "chat", display_name: id, available: true, allowed: true, qualified: false,
  recommended: false, capabilities: ["completion"], registry_status: "", upstream_model: id,
  generation_status: "supported", ...over,
});

/** Projection backend de l'état réel du 02/11/2026. */
function catalog(over: Partial<any> = {}) {
  return {
    chat: [
      chatEntry("qwen3:30b-instruct", { qualified: true, recommended: true }),
      chatEntry("mistral-small3.2:24b"),
      chatEntry("gpt-oss:20b", { allowed: false, generation_status: "unsupported",
                                 reason: "model_generation_not_supported" }),
      chatEntry("qwen3:30b-a3b-thinking-2507-q4_K_M", { allowed: false,
        generation_status: "unsupported", reason: "model_generation_not_supported" }),
      chatEntry("semx-minicpm-v4.5:8b-q8_0", { allowed: false, generation_status: "unsafe_output",
                                               reason: "model_output_policy_rejected" }),
      chatEntry("deepseek-r1:32b", { allowed: false, generation_status: "not_verified",
                                     reason: "model_generation_not_verified" }),
    ],
    embeddings: [
      { id: "legacy-768", kind: "embedding", display_name: "legacy-768", available: true,
        allowed: true, qualified: true, recommended: true, capabilities: ["embedding"],
        registry_status: "ready", upstream_model: "nomic-embed-text:latest", dimension: 768 },
    ],
    available: true, stale: false, qualification_revision: "2026-11-02.1",
    expected_embedding_dimension: 768,
    default_chat_model: "qwen3:30b-instruct", default_chat_status: "available",
    recommended_chat_reason: "Choix par défaut opérationnel de la plateforme.",
    generation_registry_available: true,
    chat_providers: { ollama: { selectable: true, reason: "" },
                      },
    ...over,
  };
}

const POLICY = { llm_provider: "ollama", llm_model: "qwen3:30b-instruct",
                 embedding_provider: "ai_gateway", embedding_model: "legacy-768", can_edit: true };

function route(map: Record<string, any>) {
  apiFetch.mockImplementation((url: string) => {
    for (const [frag, value] of Object.entries(map)) {
      if (url.includes(frag)) return Promise.resolve(value);
    }
    return Promise.reject(new Error(`URL non routée: ${url}`));
  });
}

const noop = async () => {};
const chatSelect = () => screen.getByLabelText("Modèle de génération") as HTMLSelectElement;
const saveButton = (label: RegExp) => screen.getByRole("button", { name: label }) as HTMLButtonElement;

function renderCreate(cat: any = catalog(), policy: any = POLICY) {
  route({ "model-catalog": cat, "capabilities": { ragspace_model_policy: policy } });
  return render(<SpaceEditModal space={null} onClose={noop} onCreate={noop} onPatch={noop} />);
}

function renderEdit(llm: Partial<any>, cat: any = catalog()) {
  route({
    "model-catalog": cat,
    "llm-settings": { llm_provider: "ollama", llm_model: "qwen3:30b-instruct",
                      embedding_provider: "ai_gateway", embedding_model: "legacy-768",
                      can_edit_model_configuration: true,
                      documents: { total: 2, ready: 2, failed: 0, reindex_required: 0 }, ...llm },
  });
  return render(<SpaceEditModal space={{ id: "s1", name: "RH" } as any} onClose={noop}
                                onCreate={noop} onPatch={noop} />);
}

beforeEach(() => {
  apiFetch.mockReset();
});

describe("catalogue de chat filtré", () => {
  it("ne propose que les modèles servis en génération", async () => {
    renderCreate();
    await waitFor(() => expect(chatSelect().value).toBe("qwen3:30b-instruct"));
    const values = [...chatSelect().options].map((o) => o.value);
    expect(values).toEqual(["qwen3:30b-instruct", "mistral-small3.2:24b"]);
    for (const banned of ["gpt-oss:20b", "qwen3:30b-a3b-thinking-2507-q4_K_M",
                          "semx-minicpm-v4.5:8b-q8_0", "deepseek-r1:32b"]) {
      expect(values).not.toContain(banned);
    }
  });

  it("présélection = modèle recommandé, avec une justification honnête", async () => {
    renderCreate();
    await waitFor(() => expect(chatSelect().value).toBe("qwen3:30b-instruct"));
    expect(chatSelect().selectedOptions[0].textContent).toContain("Recommandé");
    expect(screen.getByText(/par défaut opérationnel/)).toBeTruthy();
    expect(screen.queryByText(/meilleur/i)).toBeNull();
  });

  it("aucun modèle compatible : état explicite et enregistrement bloqué", async () => {
    const cat = catalog({ chat: catalog().chat.map((e: any) => ({ ...e, allowed: false, recommended: false,
      generation_status: "not_verified", reason: "model_generation_not_verified" })),
      default_chat_status: "model_generation_not_verified", generation_registry_available: false });
    renderCreate(cat);
    await waitFor(() => expect(screen.getByTestId("no-compatible-chat-model")).toBeTruthy());
    fireEvent.change(screen.getByLabelText("Nom de l'espace"), { target: { value: "X" } });
    expect(saveButton(/Créer/).disabled).toBe(true);
  });

  it("défaut indisponible : signalé, jamais remplacé par un autre modèle", async () => {
    const cat = catalog({ default_chat_model: "gpt-oss:20b",
                          default_chat_status: "model_generation_not_supported",
                          chat: catalog().chat.map((e: any) => ({ ...e, recommended: false })) });
    renderCreate(cat, { ...POLICY, llm_model: "gpt-oss:20b" });
    await waitFor(() => expect(screen.getByTestId("default-chat-model-unavailable")).toBeTruthy());
    expect(chatSelect().value).toBe("gpt-oss:20b");
    expect(screen.getByTestId("chat-model-unusable").textContent).toMatch(/ne peut pas être utilisé/);
    fireEvent.change(screen.getByLabelText("Nom de l'espace"), { target: { value: "X" } });
    expect(saveButton(/Créer/).disabled).toBe(true);
    fireEvent.change(chatSelect(), { target: { value: "mistral-small3.2:24b" } });
    expect(saveButton(/Créer/).disabled).toBe(false);
  });
});

describe("espace historique", () => {
  it("modèle devenu incompatible : conservé, signalé, enregistrable sans changement", async () => {
    renderEdit({ llm_model: "gpt-oss:20b" });
    await waitFor(() => expect(chatSelect().value).toBe("gpt-oss:20b"));
    expect(screen.getByTestId("chat-model-unusable").textContent).toMatch(/Sélectionnez un modèle compatible/);
    expect(saveButton(/Enregistrer/).disabled).toBe(false);
  });

  it("raisonnement exposé : motif dédié", async () => {
    renderEdit({ llm_model: "semx-minicpm-v4.5:8b-q8_0" });
    await waitFor(() => expect(screen.getByTestId("chat-model-unusable").textContent)
      .toMatch(/raisonnement interne/));
  });

  it("modèle disparu du catalogue : motif dédié", async () => {
    renderEdit({ llm_model: "modele-retire:1b" });
    await waitFor(() => expect(screen.getByTestId("chat-model-unusable").textContent)
      .toMatch(/n'est plus publié/));
  });

});
