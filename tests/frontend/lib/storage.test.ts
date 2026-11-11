import { describe, it, expect, beforeEach } from "vitest";
import {
  getStoredWorkspaceId,
  setStoredWorkspaceId,
  getStoredRagSpaceIdForWorkspace,
  setStoredRagSpaceIdForWorkspace,
  getStoredThreadIdForRagSpace,
  setStoredThreadIdForRagSpace,
} from "@/app/chat/_lib/storage";

describe("chat/_lib/storage", () => {
  beforeEach(() => window.localStorage.clear());

  describe("workspace id", () => {
    it("renvoie null si absent", () => {
      expect(getStoredWorkspaceId()).toBeNull();
    });
    it("round-trip d'un entier", () => {
      setStoredWorkspaceId(42);
      expect(getStoredWorkspaceId()).toBe(42);
    });
    it("renvoie null si la valeur stockée n'est pas un entier", () => {
      window.localStorage.setItem("ragatm_selected_workspace_id", "abc");
      expect(getStoredWorkspaceId()).toBeNull();
    });
  });

  describe("ragspace par workspace", () => {
    it("round-trip par workspace", () => {
      setStoredRagSpaceIdForWorkspace(1, "rs-1");
      expect(getStoredRagSpaceIdForWorkspace(1)).toBe("rs-1");
    });
    it("renvoie '' si aucun mapping", () => {
      expect(getStoredRagSpaceIdForWorkspace(99)).toBe("");
    });
    it("une valeur vide supprime l'entrée", () => {
      setStoredRagSpaceIdForWorkspace(1, "rs-1");
      setStoredRagSpaceIdForWorkspace(1, "   ");
      expect(getStoredRagSpaceIdForWorkspace(1)).toBe("");
    });
    it("isole les workspaces", () => {
      setStoredRagSpaceIdForWorkspace(1, "rs-1");
      setStoredRagSpaceIdForWorkspace(2, "rs-2");
      expect(getStoredRagSpaceIdForWorkspace(1)).toBe("rs-1");
      expect(getStoredRagSpaceIdForWorkspace(2)).toBe("rs-2");
    });
  });

  describe("thread par ragspace", () => {
    it("round-trip", () => {
      setStoredThreadIdForRagSpace("rs-1", "thread-1");
      expect(getStoredThreadIdForRagSpace("rs-1")).toBe("thread-1");
    });
    it("ragSpaceId vide = no-op", () => {
      setStoredThreadIdForRagSpace("  ", "thread-1");
      expect(getStoredThreadIdForRagSpace("")).toBe("");
    });
    it("threadId vide supprime l'entrée", () => {
      setStoredThreadIdForRagSpace("rs-1", "thread-1");
      setStoredThreadIdForRagSpace("rs-1", "");
      expect(getStoredThreadIdForRagSpace("rs-1")).toBe("");
    });
  });
});
