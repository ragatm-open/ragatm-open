const LS_WS_ID = "ragatm_selected_workspace_id";
const LS_RS_BY_WS = "ragatm_ragspace_by_workspace";
const LS_THREAD_BY_RS = "ragatm_thread_by_ragspace";

function safeJsonParse<T>(s: string | null, fallback: T): T {
  try {
    if (!s) return fallback;
    return JSON.parse(s) as T;
  } catch {
    return fallback;
  }
}

export function getStoredWorkspaceId(): number | null {
  try {
    const stored = localStorage.getItem(LS_WS_ID);
    const parsed = stored ? parseInt(stored, 10) : NaN;
    return Number.isFinite(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

export function setStoredWorkspaceId(id: number) {
  try {
    localStorage.setItem(LS_WS_ID, String(id));
  } catch {
    // ignore
  }
}

/**
 * Persist the last selected RagSpace per workspace.
 * This makes "switch workspace → remember last ragspace" feel natural.
 */
export function getStoredRagSpaceIdForWorkspace(workspaceId: number): string {
  try {
    const map = safeJsonParse<Record<string, string>>(localStorage.getItem(LS_RS_BY_WS), {});
    return map[String(workspaceId)] || "";
  } catch {
    return "";
  }
}

export function setStoredRagSpaceIdForWorkspace(workspaceId: number, ragSpaceId: string) {
  try {
    const map = safeJsonParse<Record<string, string>>(localStorage.getItem(LS_RS_BY_WS), {});
    const key = String(workspaceId);
    const v = (ragSpaceId || "").trim();
    if (v) map[key] = v;
    else delete map[key];
    localStorage.setItem(LS_RS_BY_WS, JSON.stringify(map));
  } catch {
    // ignore
  }
}

export function getStoredThreadIdForRagSpace(ragSpaceId: string): string {
  try {
    const map = safeJsonParse<Record<string, string>>(localStorage.getItem(LS_THREAD_BY_RS), {});
    return map[ragSpaceId] || "";
  } catch {
    return "";
  }
}

export function setStoredThreadIdForRagSpace(ragSpaceId: string, threadId: string) {
  try {
    const map = safeJsonParse<Record<string, string>>(localStorage.getItem(LS_THREAD_BY_RS), {});
    const rs = (ragSpaceId || "").trim();
    if (!rs) return;

    const t = (threadId || "").trim();
    if (t) map[rs] = t;
    else delete map[rs];

    localStorage.setItem(LS_THREAD_BY_RS, JSON.stringify(map));
  } catch {
    // ignore
  }
}
