/**
 * Résolution de la locale — table de vérité de la doctrine i18n.
 * Ordre : preferred (profil) > cookie > Accept-Language > fr.
 */
import { describe, it, expect } from "vitest";
import { resolveLocale, localeFromAcceptLanguage } from "@/i18n/locale";

describe("resolveLocale", () => {
  it("profil=en, cookie=fr → en (le profil l'emporte)", () => {
    expect(resolveLocale({ preferred: "en", cookie: "fr" })).toBe("en");
  });

  it("profil=fr, cookie=en → fr", () => {
    expect(resolveLocale({ preferred: "fr", cookie: "en" })).toBe("fr");
  });

  it("pas de profil, cookie=en → en", () => {
    expect(resolveLocale({ cookie: "en" })).toBe("en");
  });

  it("pas de cookie, navigateur=en-US → en", () => {
    expect(resolveLocale({ acceptLanguage: "en-US,en;q=0.9" })).toBe("en");
  });

  it("pas de cookie, navigateur=de-DE → fr (non supporté ⇒ défaut)", () => {
    expect(resolveLocale({ acceptLanguage: "de-DE,de;q=0.9" })).toBe("fr");
  });

  it("cookie invalide → fr", () => {
    expect(resolveLocale({ cookie: "zz" })).toBe("fr");
  });

  it("preferred invalide mais cookie valide → cookie", () => {
    expect(resolveLocale({ preferred: "de", cookie: "en" })).toBe("en");
  });

  it("aucun signal → fr", () => {
    expect(resolveLocale({})).toBe("fr");
  });
});

describe("localeFromAcceptLanguage", () => {
  it("respecte les poids q", () => {
    expect(localeFromAcceptLanguage("fr;q=0.2, en;q=0.9")).toBe("en");
  });
  it("ramène une variante régionale à sa base", () => {
    expect(localeFromAcceptLanguage("en-GB")).toBe("en");
  });
  it("null si aucune locale supportée", () => {
    expect(localeFromAcceptLanguage("es-ES,it;q=0.5")).toBeNull();
  });
  it("null sur en-tête vide", () => {
    expect(localeFromAcceptLanguage("")).toBeNull();
  });
});
