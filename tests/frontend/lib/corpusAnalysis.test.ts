import { describe, expect, it } from "vitest";

import {
  isTerminalAnalysisStatus,
  TERMINAL_ANALYSIS_STATUSES,
  type AnalysisStatus,
} from "@/lib/api";

describe("statuts d'analyse", () => {
  it("reconnaît les statuts terminaux", () => {
    for (const s of ["succeeded", "partial", "failed", "cancelled"] as AnalysisStatus[]) {
      expect(isTerminalAnalysisStatus(s)).toBe(true);
    }
  });

  it("ne considère pas un job en cours comme terminal", () => {
    for (const s of ["pending", "running"] as AnalysisStatus[]) {
      expect(isTerminalAnalysisStatus(s)).toBe(false);
    }
  });

  it("un statut inconnu n'est jamais terminal (un poller ne s'arrête pas à tort)", () => {
    expect(isTerminalAnalysisStatus("unknown")).toBe(false);
    expect(isTerminalAnalysisStatus("")).toBe(false);
  });

  it("la liste des statuts terminaux est exactement celle du backend", () => {
    expect([...TERMINAL_ANALYSIS_STATUSES].sort()).toEqual([
      "cancelled",
      "failed",
      "partial",
      "succeeded",
    ]);
  });
});
