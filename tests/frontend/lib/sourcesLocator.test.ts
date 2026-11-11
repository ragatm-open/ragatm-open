import { describe, expect, it } from "vitest";

import { srcLocator, srcLocatorLabel, srcPage, srcSlide, srcSheet } from "@/app/chat/_lib/sources";

/**
 * repère de provenance générique (`meta.locator`).
 *
 * Un locator n'est PAS une page : `srcPage` reste nul pour une source sans page, et
 * un locator ne fabrique jamais de numéro de page ni de diapositive. Le libellé humain
 * se compose de `path` et `identifier` (les deux, l'un, ou rien) ; `type` reste
 * technique et n'est jamais affiché.
 */

const base = {
  rank: 1,
  document_id: "d-struct",
  chunk_id: "c-1",
  filename: "projet.dat",
  page_number: null,
  snippet: "Clause sept.",
};

describe("srcLocator — lecture tolérante", () => {
  it("expose les trois champs quand ils sont présents", () => {
    const s = { ...base, meta: { locator: { type: "element", path: "/projet/lot[3]", identifier: "CL-42" } } };
    expect(srcLocator(s)).toEqual({ type: "element", path: "/projet/lot[3]", identifier: "CL-42" });
  });

  it("null quand la source n'a pas de locator (ancien contrat, PDF, classeur)", () => {
    expect(srcLocator({ ...base, meta: {} })).toBeNull();
    expect(srcLocator({ ...base } as any)).toBeNull();
    expect(srcLocator(null)).toBeNull();
    expect(srcLocator(undefined)).toBeNull();
  });

  it("robuste aux valeurs malformées : jamais d'exception, jamais de champ inventé", () => {
    expect(srcLocator({ ...base, meta: { locator: null } })).toBeNull();
    expect(srcLocator({ ...base, meta: { locator: "chaine" as any } })).toBeNull();
    expect(srcLocator({ ...base, meta: { locator: 42 as any } })).toBeNull();
    expect(srcLocator({ ...base, meta: { locator: {} } })).toBeNull();
    expect(srcLocator({ ...base, meta: { locator: { path: 12 as any, identifier: null } } })).toBeNull();
    expect(srcLocator({ ...base, meta: { locator: { path: "   ", identifier: "" } } })).toBeNull();
  });

  it("ne garde que les chaînes non vides, épurées", () => {
    expect(srcLocator({ ...base, meta: { locator: { path: "  /a/b  ", identifier: 7 as any } } })).toEqual({
      path: "/a/b",
    });
  });
});

describe("srcLocatorLabel — libellé humain", () => {
  it("chemin + identifiant", () => {
    const s = { ...base, meta: { locator: { path: "/projet/lot[3]", identifier: "CL-42" } } };
    expect(srcLocatorLabel(s)).toBe("/projet/lot[3] · CL-42");
  });

  it("chemin seul", () => {
    expect(srcLocatorLabel({ ...base, meta: { locator: { path: "/projet/lot[3]" } } })).toBe("/projet/lot[3]");
  });

  it("identifiant seul", () => {
    expect(srcLocatorLabel({ ...base, meta: { locator: { identifier: "CL-42" } } })).toBe("CL-42");
  });

  it("type seul : aucun libellé (le type est technique, pas un repère)", () => {
    expect(srcLocatorLabel({ ...base, meta: { locator: { type: "element" } } })).toBeNull();
  });

  it("aucun locator ou locator malformé : null", () => {
    expect(srcLocatorLabel({ ...base, meta: {} })).toBeNull();
    expect(srcLocatorLabel({ ...base, meta: { locator: "x" as any } })).toBeNull();
    expect(srcLocatorLabel(null)).toBeNull();
  });
});

describe("un locator n'est ni une page, ni une diapositive, ni une feuille", () => {
  const s = { ...base, meta: { locator: { path: "/projet/lot[3]", identifier: "CL-42" } } };
  it("srcPage reste nul", () => {
    expect(srcPage(s)).toBeNull();
  });
  it("srcSlide et srcSheet restent nuls", () => {
    expect(srcSlide(s)).toBeNull();
    expect(srcSheet(s)).toBeNull();
  });
  it("un PDF paginé sans locator garde sa page et n'a aucun libellé de repère", () => {
    const pdf = { ...base, filename: "rapport.pdf", page_number: 12, meta: {} };
    expect(srcPage(pdf)).toBe(12);
    expect(srcLocatorLabel(pdf)).toBeNull();
  });
});
