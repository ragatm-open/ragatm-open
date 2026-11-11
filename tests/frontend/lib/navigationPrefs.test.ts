/**
 * Favoris et récents par utilisateur (localStorage).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  FAVORITES_MAX,
  RECENTS_MAX,
  favoritesKey,
  getFavorites,
  getRecents,
  pushRecent,
  recentsKey,
  toggleFavorite,
  visibleIds,
} from "@/lib/navigationPrefs";

describe("navigationPrefs", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.restoreAllMocks());

  it("clés suffixées par l'utilisateur", () => {
    expect(favoritesKey("u1")).toBe("ragatm_nav_favorites:u1");
    expect(recentsKey(7)).toBe("ragatm_nav_recents:7");
  });

  it("isole les favoris de deux utilisateurs du même navigateur", () => {
    toggleFavorite("u1", "A");
    toggleFavorite("u2", "B");
    expect(getFavorites("u1")).toEqual(["A"]);
    expect(getFavorites("u2")).toEqual(["B"]);
    expect(getFavorites(null)).toEqual([]);
    expect(toggleFavorite(null, "C")).toEqual([]);
  });

  it("toggle ajoute puis retire ; refuse un ajout au plafond", () => {
    expect(toggleFavorite("u1", "A")).toEqual(["A"]);
    expect(toggleFavorite("u1", "A")).toEqual([]);
    for (let i = 0; i < FAVORITES_MAX; i++) toggleFavorite("u1", `s${i}`);
    expect(getFavorites("u1")).toHaveLength(FAVORITES_MAX);
    expect(toggleFavorite("u1", "extra")).not.toContain("extra");
    expect(getFavorites("u1")).toHaveLength(FAVORITES_MAX);
  });

  it("récents : plus récent d'abord, dédoublonnés, plafonnés", () => {
    pushRecent("u1", "A");
    pushRecent("u1", "B");
    pushRecent("u1", "A");
    expect(getRecents("u1")).toEqual(["A", "B"]);
    for (let i = 0; i < 20; i++) pushRecent("u1", `s${i}`);
    expect(getRecents("u1")).toHaveLength(RECENTS_MAX);
    expect(getRecents("u1")[0]).toBe("s19");
  });

  it("ne stocke que des identifiants (aucun nom, aucun contenu)", () => {
    toggleFavorite("u1", "A");
    pushRecent("u1", "B");
    expect(JSON.parse(localStorage.getItem("ragatm_nav_favorites:u1")!)).toEqual(["A"]);
    expect(JSON.parse(localStorage.getItem("ragatm_nav_recents:u1")!)).toEqual(["B"]);
  });

  it("valeur corrompue ou non-tableau : liste vide", () => {
    localStorage.setItem("ragatm_nav_favorites:u1", "{oops");
    expect(getFavorites("u1")).toEqual([]);
    localStorage.setItem("ragatm_nav_favorites:u1", JSON.stringify({ a: 1 }));
    expect(getFavorites("u1")).toEqual([]);
    localStorage.setItem("ragatm_nav_favorites:u1", JSON.stringify(["A", 3, "A", "", "B"]));
    expect(getFavorites("u1")).toEqual(["A", "B"]);
  });

  it("localStorage indisponible : aucune exception", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("denied");
    });
    expect(getFavorites("u1")).toEqual([]);
    expect(() => toggleFavorite("u1", "A")).not.toThrow();
    expect(() => pushRecent("u1", "A")).not.toThrow();
  });

  it("affichage = intersection avec les espaces visibles, ordre stocké", () => {
    expect(visibleIds(["C", "gone", "A"], [{ id: "A" }, { id: "B" }, { id: "C" }])).toEqual(["C", "A"]);
  });
});
