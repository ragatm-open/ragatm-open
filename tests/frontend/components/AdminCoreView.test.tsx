import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: any) => (
    <a href={typeof href === "string" ? href : "#"} {...rest}>
      {children}
    </a>
  ),
}));

import { AdminCoreView } from "@/app/admin-core/_components/AdminCoreView";
import type { CoreConfigEffective, CoreHealth, CoreAuditPage } from "@/lib/coreConfig";

// Forme : le Core n'est pas vérifié (`ok: null`), la santé
// Gateway vient des sondes réelles.
const HEALTH: CoreHealth = {
  core_health: { ok: null, verified: false, embedding_dim: 768 },
  gateway_configured: true,
  gateway_enabled: false,
  gateway_health: {
    status: "DEGRADED",
    services: { vision: "AVAILABLE", structured_extract: "DEGRADED", asr: "UNAVAILABLE", rerank: "UNKNOWN" },
    probed_at: 1794046541.5,
    cached: true,
  },
  fallback_enabled: true,
};

const EFFECTIVE: CoreConfigEffective = {
  environment: "prod",
  read_only: true,
  config: [
    { key: "AI_GATEWAY_TIMEOUT_SECONDS", effective_value: false, source: "settings", editable: false, sensitive: false, redacted: false },
    { key: "AI_GATEWAY_BASE_URL", effective_value: "http://…:8100", source: "settings", editable: false, sensitive: true, redacted: true },
    { key: "RAG_EMBEDDING_DIM", effective_value: 768, source: "settings", editable: false, sensitive: false, redacted: false },
  ],
};

const AUDIT_EMPTY: CoreAuditPage = { count: 0, limit: 50, offset: 0, results: [] };

const base = { loading: false, error: null, effective: EFFECTIVE, health: HEALTH, audit: AUDIT_EMPTY };

describe("AdminCoreView", () => {
  it("affiche la santé Core/Gateway sans « OK » non prouvé", () => {
    render(<AdminCoreView {...base} />);
    // core_health.ok === null / verified === false → « Non vérifié », jamais « OK »
    expect(screen.getByText("Non vérifié")).toBeInTheDocument();
    expect(screen.queryByText("OK")).toBeNull();
    expect(screen.queryByText("KO")).toBeNull();
    expect(screen.getByText("Santé AI Gateway")).toBeInTheDocument();
    expect(screen.getByText("Service vision")).toBeInTheDocument();
    expect(screen.getByText("Disponible")).toBeInTheDocument();
    expect(screen.getAllByText("Dégradé")).toHaveLength(2); // global + structured_extract
    expect(screen.getByText("Indisponible")).toBeInTheDocument();
    expect(screen.getByText("Inconnu")).toBeInTheDocument();
    expect(screen.getByText("Gateway configuré")).toBeInTheDocument();
    expect(screen.getByText("Gateway activé")).toBeInTheDocument();
    expect(screen.getByText("activé")).toBeInTheDocument(); // fallback
    // embedding dim 768 apparaît (santé + config)
    expect(screen.getAllByText("768").length).toBeGreaterThanOrEqual(1);
  });

  it("santé Gateway « déclarée » → jamais affichée comme disponible", () => {
    render(<AdminCoreView {...base} health={{ ...HEALTH, gateway_health: { status: "DECLARED", services: { embeddings: "DECLARED" } } }} />);
    expect(screen.getAllByText("Déclaré (non testé)").length).toBeGreaterThan(0);
    expect(screen.queryByText("Disponible")).not.toBeInTheDocument();
  });

  it("santé Gateway absente → « Inconnu » (jamais disponible par défaut)", () => {
    render(<AdminCoreView {...base} health={{ ...HEALTH, gateway_health: null }} />);
    expect(screen.getByText("Inconnu")).toBeInTheDocument();
    expect(screen.queryByText("Disponible")).toBeNull();
  });

  it("santé Gateway au statut nul → « Inconnu »", () => {
    render(<AdminCoreView {...base} health={{ ...HEALTH, gateway_health: { status: null, services: {} } }} />);
    expect(screen.getByText("Inconnu")).toBeInTheDocument();
  });

  it("Core vérifié par le backend → OK conservé", () => {
    render(<AdminCoreView {...base} health={{ ...HEALTH, core_health: { ok: true, verified: true, embedding_dim: 768 } }} />);
    expect(screen.getByText("OK")).toBeInTheDocument();
  });

  it("lien visible vers les capacités documentaires", () => {
    render(<AdminCoreView {...base} />);
    const link = screen.getByRole("link", { name: /Capacités documentaires/ });
    expect(link).toHaveAttribute("href", "/admin-core/document-capabilities");
  });

  it("affiche la config effective et conserve la valeur redacted", () => {
    render(<AdminCoreView {...base} />);
    expect(screen.getByText("AI_GATEWAY_TIMEOUT_SECONDS")).toBeInTheDocument();
    expect(screen.getByText("AI_GATEWAY_BASE_URL")).toBeInTheDocument();
    // valeur masquée affichée telle quelle (jamais l'hôte réel)
    expect(screen.getByText("http://…:8100")).toBeInTheDocument();
    expect(screen.getByText("masqué")).toBeInTheDocument();
    // aucun hôte/IP réel ne doit apparaître
    expect(screen.queryByText(/127\.0\.0\.1|10\.0\.0\.|\.internal/)).toBeNull();
  });

  it("est en lecture seule : aucun bouton d'écriture", () => {
    render(<AdminCoreView {...base} />);
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });

  it("état loading", () => {
    render(<AdminCoreView loading error={null} effective={null} health={null} audit={null} />);
    expect(screen.getByRole("status")).toHaveTextContent("Chargement…");
  });

  it("état error", () => {
    render(<AdminCoreView loading={false} error="boom" effective={null} health={null} audit={null} />);
    expect(screen.getByRole("alert")).toHaveTextContent("boom");
  });

  it("audit vide affiche un message", () => {
    render(<AdminCoreView {...base} />);
    expect(screen.getByText("Aucun changement enregistré.")).toBeInTheDocument();
  });

  it("affiche le bandeau 'diagnostic-only' quand l'overlay est diagnostique", () => {
    const eff: CoreConfigEffective = {
      ...EFFECTIVE,
      diagnostic_overlay_only: true,
      db_overlay_runtime_active: false,
      runtime_source_of_truth: "settings",
      warning: "DB overrides are diagnostic-only and do not affect runtime behavior.",
    };
    render(<AdminCoreView {...base} effective={eff} />);
    const note = screen.getByRole("note");
    expect(note).toHaveTextContent("diagnostic-only");
    expect(note).toHaveTextContent("settings");
  });

  it("pas de bandeau si l'overlay n'est pas marqué diagnostique", () => {
    render(<AdminCoreView {...base} />);
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("affiche un bandeau d'alerte quand l'overlay est ACTIF au runtime", () => {
    const eff: CoreConfigEffective = {
      ...EFFECTIVE,
      diagnostic_overlay_only: false,
      db_overlay_runtime_active: true,
      runtime_source_of_truth: "db_overlay",
      warning: "DB overrides are ACTIVE at runtime (CORE_CONFIG_RUNTIME_ENABLED=1).",
    };
    render(<AdminCoreView {...base} effective={eff} />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("ACTIVE at runtime");
    expect(alert).toHaveTextContent("db_overlay");
    // pas de bandeau "diagnostic-only" (note) en même temps
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("audit avec événement masque l'email (username only)", () => {
    const audit: CoreAuditPage = {
      count: 1, limit: 50, offset: 0,
      results: [{
        id: "e1", created_at: "2026-06-17T10:00:00Z", user: "root", user_id: 1,
        workspace_id: 1, rag_space_id: "rs-1",
        chat: { old_provider: "ollama", old_model: "llama3.1:8b", new_provider: "ollama", new_model: "qwen3:8b" },
        embeddings: { old_provider: "ai_gateway", old_model: "legacy-768", new_provider: "ai_gateway", new_model: "fr-en-retrieval" },
      }],
    };
    render(<AdminCoreView {...base} audit={audit} />);
    expect(screen.getByText("root")).toBeInTheDocument();
    expect(screen.queryByText(/@/)).toBeNull(); // pas d'email
  });
});
