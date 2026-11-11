import { describe, it, expect, vi, beforeEach } from "vitest";

// On mocke le client bas niveau pour prouver que coreConfig n'utilise QUE des GET.
const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const apiDelete = vi.fn();

vi.mock("@/lib/api", () => ({
  apiGet: (...a: unknown[]) => apiGet(...a),
  apiPost: (...a: unknown[]) => apiPost(...a),
  apiPatch: (...a: unknown[]) => apiPatch(...a),
  apiDelete: (...a: unknown[]) => apiDelete(...a),
  apiFetch: vi.fn(),
}));

import { getEffectiveConfig, getCoreHealth, getCoreAudit, getDocumentCapabilities } from "@/lib/coreConfig";

describe("coreConfig (lecture seule)", () => {
  beforeEach(() => {
    apiGet.mockReset().mockResolvedValue({});
    apiPost.mockReset();
    apiPatch.mockReset();
    apiDelete.mockReset();
  });

  it("getEffectiveConfig appelle GET sur l'endpoint effective", async () => {
    await getEffectiveConfig();
    expect(apiGet).toHaveBeenCalledWith("/api/v1/admin/core-config/effective/");
  });

  it("getCoreHealth appelle GET sur l'endpoint health", async () => {
    await getCoreHealth();
    expect(apiGet).toHaveBeenCalledWith("/api/v1/admin/core-config/health/");
  });

  it("getCoreAudit construit la query et appelle GET", async () => {
    await getCoreAudit({ limit: 50, offset: 10 });
    expect(apiGet).toHaveBeenCalledWith("/api/v1/admin/core-config/audit/?limit=50&offset=10");
  });

  it("getCoreAudit sans option n'ajoute pas de query", async () => {
    await getCoreAudit();
    expect(apiGet).toHaveBeenCalledWith("/api/v1/admin/core-config/audit/");
  });

  it("getDocumentCapabilities appelle GET sans query par défaut", async () => {
    await getDocumentCapabilities();
    expect(apiGet).toHaveBeenCalledWith("/api/v1/admin/core-config/document-capabilities/");
  });

  it("getDocumentCapabilities transmet ragspace_id et refresh=1", async () => {
    await getDocumentCapabilities({ ragspaceId: " 11111111-2222-3333-4444-555555555555 ", refresh: true });
    expect(apiGet).toHaveBeenCalledWith(
      "/api/v1/admin/core-config/document-capabilities/?ragspace_id=11111111-2222-3333-4444-555555555555&refresh=1"
    );
    await getDocumentCapabilities({ ragspaceId: "", refresh: false });
    expect(apiGet).toHaveBeenLastCalledWith("/api/v1/admin/core-config/document-capabilities/");
  });

  it("aucune écriture (POST/PATCH/DELETE) n'est jamais émise", async () => {
    await getEffectiveConfig();
    await getCoreHealth();
    await getCoreAudit();
    expect(apiPost).not.toHaveBeenCalled();
    expect(apiPatch).not.toHaveBeenCalled();
    expect(apiDelete).not.toHaveBeenCalled();
  });
});
