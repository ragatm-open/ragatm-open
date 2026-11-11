/**
 * Conversions d'unités de quota. La valeur canonique est TOUJOURS en octets ;
 * l'UI convertit sans introduire de flottant comme quantité stockée.
 */
import { describe, it, expect } from "vitest";
import {
  unitToBytes,
  bytesToUnit,
  pickUnit,
  bytesToInputValue,
} from "@/lib/quota";

describe("quota conversions", () => {
  it("convertit unité → octets (binaire 1024)", () => {
    expect(unitToBytes(10, "GB")).toBe(10 * 1024 ** 3);
    expect(unitToBytes(1, "TB")).toBe(1024 ** 4);
    expect(unitToBytes(500, "MB")).toBe(500 * 1024 ** 2);
  });

  it("arrondit à l'entier (jamais de flottant canonique)", () => {
    expect(Number.isInteger(unitToBytes(10.5, "GB"))).toBe(true);
  });

  it("convertit octets → unité", () => {
    expect(bytesToUnit(10 * 1024 ** 3, "GB")).toBe(10);
  });

  it("choisit une unité d'affichage naturelle", () => {
    expect(pickUnit(10 * 1024 ** 3)).toBe("GB");
    expect(pickUnit(1024 ** 4)).toBe("TB");
    expect(pickUnit(500 * 1024 ** 2)).toBe("MB");
  });

  it("formate une valeur d'édition entière quand exacte", () => {
    expect(bytesToInputValue(10 * 1024 ** 3, "GB")).toBe("10");
  });
});
