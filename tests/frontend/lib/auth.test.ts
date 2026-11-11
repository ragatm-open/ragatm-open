import { describe, it, expect, afterEach } from "vitest";
import { getCsrfToken } from "@/lib/auth";

describe("lib/auth", () => {
  afterEach(() => {
    // purge des cookies posés par les tests
    for (const c of document.cookie.split(";")) {
      const name = c.split("=")[0].trim();
      if (name) document.cookie = `${name}=; max-age=0; path=/`;
    }
  });

  it("n'expose plus aucune API de stockage de token", async () => {
    const mod: any = await import("@/lib/auth");
    expect(mod.getToken).toBeUndefined();
    expect(mod.setToken).toBeUndefined();
    expect(mod.clearToken).toBeUndefined();
  });

  it("getCsrfToken renvoie null quand le cookie est absent", () => {
    expect(getCsrfToken()).toBeNull();
  });

  it("getCsrfToken lit la valeur du cookie csrftoken", () => {
    document.cookie = "csrftoken=abc123; path=/";
    expect(getCsrfToken()).toBe("abc123");
  });
});
