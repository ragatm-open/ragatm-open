import { describe, expect, it } from "vitest";

import { srcAudioTimeRange, srcLocatorLabel, srcViewerHref } from "@/app/chat/_lib/sources";

/** citation d'un passage de transcription audio. */
const base = {
  rank: 1,
  document_id: "d-audio",
  chunk_id: "c-1",
  filename: "reunion.mp3",
  page_number: null,
  snippet: "On valide le lot 3.",
};
const audioMeta = {
  source_type: "AUDIO_ASR_TRANSCRIPT",
  source_is_asr: true,
  start_ms: 755_000,
  end_ms: 828_000,
  timestamp_precision: "segment",
  locator: { type: "time_range", identifier: "00:12:35 → 00:13:48" },
};

describe("srcAudioTimeRange", () => {
  it("intervalle horodaté, précision segment", () => {
    expect(srcAudioTimeRange({ ...base, meta: audioMeta } as any)).toEqual({
      range: "00:12:35 → 00:13:48", approximate: false,
    });
  });

  it("précision à la partie => approximatif", () => {
    expect(
      srcAudioTimeRange({ ...base, meta: { ...audioMeta, timestamp_precision: "part" } } as any)
    ).toEqual({ range: "00:12:35 → 00:13:48", approximate: true });
  });

  it("null hors time_range ou sans identifiant", () => {
    expect(srcAudioTimeRange({ ...base, meta: { locator: { type: "element", identifier: "X" } } })).toBeNull();
    expect(srcAudioTimeRange({ ...base, meta: { locator: { type: "time_range" } } })).toBeNull();
    expect(srcAudioTimeRange({ ...base, meta: {} })).toBeNull();
  });

  it("le lien viewer n'ancre aucun instant de l'audio", () => {
    const href = srcViewerHref({ ...base, meta: audioMeta } as any) ?? "";
    expect(href).not.toMatch(/[?&](t|start|time|start_ms)=/);
    expect(srcLocatorLabel({ ...base, meta: audioMeta } as any)).toBe("00:12:35 → 00:13:48");
  });
});
