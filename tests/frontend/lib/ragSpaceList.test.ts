import { describe, it, expect } from "vitest";
import { upsertRagSpaceInList } from "@/components/shell/ragSpaceList";
import type { RagSpaceListItem } from "@/components/shell";

const A: RagSpaceListItem = { id: "A", name: "Alpha", llm_provider: "ollama" };
const B: RagSpaceListItem = { id: "B", name: "Beta", llm_provider: "ollama" };

describe("upsertRagSpaceInList", () => {
  it("ajoute un nouvel espace en fin de liste", () => {
    const out = upsertRagSpaceInList([A], B);
    expect(out.map((s) => s.id)).toEqual(["A", "B"]);
  });

  it("est idempotent : ré-upsert du même id ne crée pas de doublon", () => {
    const once = upsertRagSpaceInList([A], B);
    const twice = upsertRagSpaceInList(once, B);
    expect(twice.filter((s) => s.id === "B")).toHaveLength(1);
    expect(twice.map((s) => s.id)).toEqual(["A", "B"]);
  });

  it("remplace (merge) un espace existant sans créer de doublon (édition)", () => {
    const renamed: RagSpaceListItem = { id: "B", name: "Beta 2", embedding_provider: "ai_gateway" };
    const out = upsertRagSpaceInList([A, B], renamed);
    expect(out).toHaveLength(2);
    const b = out.find((s) => s.id === "B")!;
    expect(b.name).toBe("Beta 2");
    expect(b.embedding_provider).toBe("ai_gateway");
    // merge : conserve les champs non fournis par la mise à jour.
    expect(b.llm_provider).toBe("ollama");
  });

  it("ne mute pas la liste d'entrée (fonction pure)", () => {
    const input = [A];
    const out = upsertRagSpaceInList(input, B);
    expect(input).toEqual([A]);
    expect(out).not.toBe(input);
  });
});
