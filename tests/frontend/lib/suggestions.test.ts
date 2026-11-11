import { describe, it, expect } from "vitest";
import { extractSuggestions } from "@/app/chat/_lib/suggestions";

describe("extractSuggestions", () => {
  it("extrait les suggestions d'un payload d'événement", () => {
    expect(extractSuggestions({ suggested_rephrasings: ["A ?", "B ?"] })).toEqual(["A ?", "B ?"]);
  });

  it("renvoie [] quand le champ est absent (réponse valide / done sans suggestions)", () => {
    expect(extractSuggestions({})).toEqual([]);
    expect(extractSuggestions({ suggested_rephrasings: null })).toEqual([]);
    expect(extractSuggestions(undefined)).toEqual([]);
  });

  it("ignore les non-chaînes et les chaînes vides", () => {
    expect(
      extractSuggestions({ suggested_rephrasings: ["", "   ", 42, null, "Utile ?"] })
    ).toEqual(["Utile ?"]);
  });

  it("borne dure à 3 suggestions", () => {
    expect(
      extractSuggestions({ suggested_rephrasings: ["1 ?", "2 ?", "3 ?", "4 ?", "5 ?"] })
    ).toEqual(["1 ?", "2 ?", "3 ?"]);
  });

  it("n'explose pas si suggested_rephrasings n'est pas un tableau", () => {
    expect(extractSuggestions({ suggested_rephrasings: "pas un tableau" })).toEqual([]);
  });
});
