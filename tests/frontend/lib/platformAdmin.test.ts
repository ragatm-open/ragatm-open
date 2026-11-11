import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { ApiError } from "@/lib/apiError";
import {
  humanPlatformError,
  platformErrorCode,
  listPlatformUsers,
  createWorkspace,
  disableWorkspace,
  previewOwnerScopeTransfer,
} from "@/lib/platformAdmin";

function jsonResponse(body: any, status = 200) {
  return {
    ok: status < 400,
    status,
    statusText: "OK",
    headers: { get: (k: string) => (k.toLowerCase() === "content-type" ? "application/json" : null) },
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

describe("platformAdmin — messages neutres (§18)", () => {
  it("traduit les codes internes en messages FR sans exposer le code", () => {
    const e = new ApiError(422, "", { detail: "cross_workspace" }, "API 422");
    expect(platformErrorCode(e)).toBe("cross_workspace");
    const msg = humanPlatformError(e);
    expect(msg).toBe("Cette opération créerait une incohérence entre workspaces.");
    expect(msg).not.toContain("cross_workspace");
  });
  it("409 sans code connu => message de désynchronisation", () => {
    expect(humanPlatformError(new ApiError(409, "", null, ""))).toMatch(/état a changé/i);
  });
  it("erreur non-API => fallback", () => {
    expect(humanPlatformError(new Error("x"), "fallback")).toBe("fallback");
  });
});

describe("platformAdmin — appels HTTP", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [], total: 0, limit: 50, offset: 0 }));
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());
  const url = () => fetchMock.mock.calls.at(-1)![0] as string;
  const init = () => fetchMock.mock.calls.at(-1)![1] as RequestInit;

  it("users list : query params encodés", async () => {
    await listPlatformUsers({ search: "bob", is_active: true, limit: 10 });
    expect(url()).toContain("/api/v1/platform/users/?");
    expect(url()).toContain("search=bob");
    expect(url()).toContain("is_active=1");
  });
  it("create workspace : POST body name+owner", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ code: "workspace_created" }, 201));
    await createWorkspace("WS", 42);
    expect(url()).toBe("/api/v1/platform/workspaces/");
    expect(init().method).toBe("POST");
    expect(JSON.parse(init().body as string)).toEqual({ name: "WS", owner_user_id: 42 });
  });
  it("disable : POST reason", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ code: "workspace_disabled" }));
    await disableWorkspace(3, "obsolete");
    expect(url()).toBe("/api/v1/platform/workspaces/3/disable/");
    expect(JSON.parse(init().body as string)).toEqual({ reason: "obsolete" });
  });
  it("transfer preview : POST source/target", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ apply_supported: false }));
    await previewOwnerScopeTransfer({ workspace_id: 1, source_owner_membership_id: 2, target_owner_membership_id: 3 });
    expect(url()).toBe("/api/v1/platform/owner-scope-transfers/preview/");
    expect(init().method).toBe("POST");
  });
});
