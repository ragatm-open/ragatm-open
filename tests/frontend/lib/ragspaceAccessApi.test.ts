import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  listRagSpaceGrants,
  listEligibleUsers,
  grantRagSpaceAccess,
  changeRagSpaceAccessRole,
  revokeRagSpaceAccess,
} from "@/lib/ragspaceAccessApi";

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

describe("ragspaceAccessApi — appelle les bons endpoints", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue(jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  const url = () => fetchMock.mock.calls.at(-1)![0] as string;
  const init = () => fetchMock.mock.calls.at(-1)![1] as RequestInit;
  const RS = "space-1";

  it("liste les octrois (GET, trailing slash)", async () => {
    await listRagSpaceGrants(RS);
    expect(url()).toBe(`/api/v1/rag-spaces/${RS}/access/`);
    expect((init().method || "GET").toUpperCase()).toBe("GET");
  });

  it("inclut l'historique révoqué sur demande", async () => {
    await listRagSpaceGrants(RS, { includeRevoked: true });
    expect(url()).toBe(`/api/v1/rag-spaces/${RS}/access/?include_revoked=1`);
  });

  it("liste les éligibles", async () => {
    await listEligibleUsers(RS);
    expect(url()).toBe(`/api/v1/rag-spaces/${RS}/access/eligible/`);
  });

  it("octroie un rôle (POST body user_id+space_role)", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ code: "granted" }, 201));
    const res = await grantRagSpaceAccess(RS, 42, "editor");
    expect(url()).toBe(`/api/v1/rag-spaces/${RS}/access/`);
    expect(init().method).toBe("POST");
    expect(JSON.parse(init().body as string)).toEqual({ user_id: 42, space_role: "editor" });
    expect(res.code).toBe("granted");
  });

  it("change un rôle (PATCH sur l'octroi)", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ code: "role_changed" }));
    await changeRagSpaceAccessRole(RS, "grant-9", "manager");
    expect(url()).toBe(`/api/v1/rag-spaces/${RS}/access/grant-9/`);
    expect(init().method).toBe("PATCH");
    expect(JSON.parse(init().body as string)).toEqual({ space_role: "manager" });
  });

  it("révoque via DELETE (revoked_at côté serveur, jamais delete physique)", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ code: "revoked" }));
    const res = await revokeRagSpaceAccess(RS, "grant-9");
    expect(url()).toBe(`/api/v1/rag-spaces/${RS}/access/grant-9/`);
    expect(init().method).toBe("DELETE");
    expect(res.code).toBe("revoked");
  });
});
