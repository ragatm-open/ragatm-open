import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { ApiError, isApiError, apiErrorStatus, humanAccessError, isAccessDenied } from "@/lib/apiError";
import { apiGet } from "@/lib/api";

function errResponse(status: number, body: any) {
  return {
    ok: false,
    status,
    statusText: "ERR",
    headers: { get: (k: string) => (k.toLowerCase() === "content-type" ? "application/json" : null) },
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

describe("ApiError", () => {
  it("porte le statut, le code (detail) et le body", () => {
    const e = new ApiError(403, "Forbidden", { detail: "space_role_insufficient" }, "API 403 …");
    expect(isApiError(e)).toBe(true);
    expect(e.status).toBe(403);
    expect(e.code).toBe("space_role_insufficient");
    expect(apiErrorStatus(e)).toBe(403);
  });
  it("reste une Error (compat .message.includes)", () => {
    const e = new ApiError(404, "Not Found", null, "API 404 Not Found: not_found");
    expect(e).toBeInstanceOf(Error);
    expect(e.message.includes("not_found")).toBe(true);
    expect(e.code).toBeNull();
  });
});

describe("humanAccessError — messages neutres, anti-énumération", () => {
  it("404 => message indistinct (jamais « supprimé » vs « interdit »)", () => {
    expect(humanAccessError(new ApiError(404, "", null, ""))).toBe("Cet espace est introuvable ou n'est pas accessible.");
  });
  it("403 => rôle insuffisant, sans code interne", () => {
    const msg = humanAccessError(new ApiError(403, "", { detail: "space_role_insufficient" }, ""));
    expect(msg).toBe("Vous n'avez pas les droits nécessaires pour cette action.");
    expect(msg).not.toContain("space_role_insufficient");
  });
  it("autres statuts => null (message générique conservé par l'appelant)", () => {
    expect(humanAccessError(new ApiError(500, "", null, ""))).toBeNull();
    expect(humanAccessError(new Error("boom"))).toBeNull();
  });
  it("isAccessDenied ne couvre que 403/404", () => {
    expect(isAccessDenied(new ApiError(403, "", null, ""))).toBe(true);
    expect(isAccessDenied(new ApiError(404, "", null, ""))).toBe(true);
    expect(isAccessDenied(new ApiError(400, "", null, ""))).toBe(false);
  });
});

describe("apiFetch lève une ApiError porteuse du statut", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue(errResponse(404, { detail: "not_found" }));
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("expose status=404 sur l'erreur remontée", async () => {
    await expect(apiGet("/api/v1/rag-spaces/x")).rejects.toMatchObject({ status: 404 });
    try {
      await apiGet("/api/v1/rag-spaces/x");
    } catch (e) {
      expect(humanAccessError(e)).toBe("Cet espace est introuvable ou n'est pas accessible.");
    }
  });
});
