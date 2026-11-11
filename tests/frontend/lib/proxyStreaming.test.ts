import { describe, expect, it } from "vitest";

import { shouldStreamRequestBody } from "@/lib/proxyStreaming";

/** seul le dépôt audio est relayé EN FLUX par le proxy Next.js. */
describe("shouldStreamRequestBody", () => {
  it("vrai pour le dépôt audio, avec ou sans barre finale, chaîne ou segments", () => {
    expect(shouldStreamRequestBody("v1/documents/upload-audio")).toBe(true);
    expect(shouldStreamRequestBody("v1/documents/upload-audio/")).toBe(true);
    expect(shouldStreamRequestBody(["v1", "documents", "upload-audio"])).toBe(true);
    expect(shouldStreamRequestBody(["v1", "documents", "upload-audio", ""])).toBe(true);
  });

  it("faux pour toute autre route : l'upload générique reste bufferisé", () => {
    for (const p of [
      "v1/documents/upload",
      "v1/documents/upload-audio/extra",
      "v1/documents",
      "v1/chat/stream",
      "documents/upload-audio-x",
      "v2/documents/upload-audio",
      "",
    ]) {
      expect(shouldStreamRequestBody(p)).toBe(false);
    }
  });
});
