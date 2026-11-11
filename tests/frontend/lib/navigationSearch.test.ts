/**
 * Recherche de navigation — pliage, rangs, classement stable.
 */
import { describe, expect, it } from "vitest";
import { foldText, isUuid, matchRank, rankSpaces, threadHref } from "@/lib/navigationSearch";

const S = (id: string, name: string) => ({ id, name });

describe("foldText", () => {
  it("retire accents et casse", () => {
    expect(foldText("Dépenses")).toBe("depenses");
    expect(foldText("DÉPENSES")).toBe("depenses");
    expect(foldText("Ça Bâtit Où")).toBe("ca batit ou");
    expect(foldText(null)).toBe("");
  });
});

describe("matchRank", () => {
  it("0 exact, 1 préfixe, 2 début de mot, 3 sous-chaîne, null sinon", () => {
    expect(matchRank("marche", "marche")).toBe(0);
    expect(matchRank("marche lot 2", "marche")).toBe(1);
    expect(matchRank("answer-marche", "marche")).toBe(2);
    expect(matchRank("premarche", "marche")).toBe(3);
    expect(matchRank("alpha", "marche")).toBeNull();
    expect(matchRank("alpha", "")).toBeNull();
  });
  it("préfère un début de mot à une sous-chaîne antérieure", () => {
    expect(matchRank("premarche marche", "marche")).toBe(2);
  });
});

describe("rankSpaces", () => {
  const spaces = [
    S("4", "Answer-Marche-Nordlicht"),
    S("1", "Premarche"),
    S("3", "Marche lot 2"),
    S("2", "Marche"),
    S("5", "Dépenses publiques"),
    S("6", "Autre"),
  ];

  it("classe par rang de correspondance puis nom", () => {
    expect(rankSpaces(spaces, "marche").map((s) => s.id)).toEqual(["2", "3", "4", "1"]);
  });

  it("insensible aux accents et à la casse", () => {
    expect(rankSpaces(spaces, "DEPENSES").map((s) => s.id)).toEqual(["5"]);
    expect(rankSpaces(spaces, "dépen").map((s) => s.id)).toEqual(["5"]);
  });

  it("favori puis récence départagent un même rang", () => {
    const tie = [S("a", "Projet Alpha"), S("b", "Projet Beta"), S("c", "Projet Gamma")];
    expect(rankSpaces(tie, "projet").map((s) => s.id)).toEqual(["a", "b", "c"]);
    expect(rankSpaces(tie, "projet", { favorites: ["c"] }).map((s) => s.id)).toEqual(["c", "a", "b"]);
    expect(rankSpaces(tie, "projet", { recents: ["b", "a"] }).map((s) => s.id)).toEqual(["b", "a", "c"]);
  });

  it("tri stable et déterministe à nom égal (départage par id)", () => {
    const same = [S("z", "Doublon"), S("m", "Doublon"), S("a", "Doublon")];
    expect(rankSpaces(same, "doublon").map((s) => s.id)).toEqual(["a", "m", "z"]);
    expect(rankSpaces([...same].reverse(), "doublon").map((s) => s.id)).toEqual(["a", "m", "z"]);
  });

  it("requête vide : aucun résultat ; plafond respecté", () => {
    expect(rankSpaces(spaces, "   ")).toEqual([]);
    const many = Array.from({ length: 80 }, (_, i) => S(String(i).padStart(3, "0"), `Espace ${i}`));
    expect(rankSpaces(many, "espace")).toHaveLength(50);
    expect(rankSpaces(many, "espace", {}, 5)).toHaveLength(5);
  });

  it("traite la saisie comme du texte (aucune regex)", () => {
    expect(rankSpaces([S("x", "a.b (c)"), S("y", "axb")], "a.b").map((s) => s.id)).toEqual(["x"]);
    expect(rankSpaces([S("x", "a.b (c)")], "(c)").map((s) => s.id)).toEqual(["x"]);
  });
});

describe("liens et identifiants", () => {
  it("isUuid", () => {
    expect(isUuid("8f0c8a1e-2b0d-4f8e-9a3b-1c2d3e4f5a6b")).toBe(true);
    expect(isUuid("not-a-uuid")).toBe(false);
    expect(isUuid("")).toBe(false);
  });
  it("threadHref", () => {
    expect(threadHref("abc")).toBe("/chat?thread=abc");
  });
});
