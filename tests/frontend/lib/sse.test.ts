import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { readSseStream } from "@/app/chat/_lib/sse";
import * as auth from "@/lib/auth";

/** Construit une fausse Response SSE dont le body délivre les chunks fournis. */
function sseResponse(chunks: string[], init: Partial<Response> = {}) {
  const enc = new TextEncoder();
  let i = 0;
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    statusText: init.statusText ?? "OK",
    text: async () => chunks.join(""),
    body: {
      getReader() {
        return {
          read: async () => {
            if (i < chunks.length) {
              const value = enc.encode(chunks[i]);
              i += 1;
              return { value, done: false };
            }
            return { value: undefined, done: true };
          },
          cancel: async () => {},
          releaseLock: () => {},
        };
      },
    },
  } as unknown as Response;
}

describe("chat/_lib/sse readSseStream", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.spyOn(auth, "getCsrfToken").mockReturnValue(null);
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("parse un event simple avec data JSON", async () => {
    fetchMock.mockResolvedValue(sseResponse(['event: meta\ndata: {"log_id":"L1"}\n\n']));
    const onEvent = vi.fn();
    await readSseStream({ url: "/x", body: {}, onEvent });
    expect(onEvent).toHaveBeenCalledWith("meta", { log_id: "L1" });
  });

  it("injecte le log_id mémorisé dans l'event done", async () => {
    fetchMock.mockResolvedValue(
      sseResponse(['event: meta\ndata: {"log_id":"L1"}\n\n', 'event: done\ndata: {"answer":"ok"}\n\n'])
    );
    const calls: any[] = [];
    await readSseStream({ url: "/x", body: {}, onEvent: (e, d) => calls.push([e, d]) });
    const done = calls.find(([e]) => e === "done");
    expect(done[1].log_id).toBe("L1");
  });

  it("mémorise aussi logId en camelCase", async () => {
    fetchMock.mockResolvedValue(
      sseResponse(['event: meta\ndata: {"logId":"L2"}\n\n', 'event: done\ndata: {}\n\n'])
    );
    const calls: any[] = [];
    await readSseStream({ url: "/x", body: {}, onEvent: (e, d) => calls.push([e, d]) });
    const done = calls.find(([e]) => e === "done");
    expect(done[1].log_id).toBe("L2");
  });

  it("reconstruit une frame coupée sur deux chunks", async () => {
    fetchMock.mockResolvedValue(sseResponse(["event: token\nda", 'ta: {"delta":"Hi"}\n\n']));
    const onEvent = vi.fn();
    await readSseStream({ url: "/x", body: {}, onEvent });
    expect(onEvent).toHaveBeenCalledWith("token", { delta: "Hi" });
  });

  it("ignore une frame data vide", async () => {
    fetchMock.mockResolvedValue(sseResponse(["event: ping\ndata:\n\n"]));
    const onEvent = vi.fn();
    await readSseStream({ url: "/x", body: {}, onEvent });
    expect(onEvent).not.toHaveBeenCalled();
  });

  it("passe la donnée brute si data non-JSON", async () => {
    fetchMock.mockResolvedValue(sseResponse(["event: token\ndata: hello\n\n"]));
    const onEvent = vi.fn();
    await readSseStream({ url: "/x", body: {}, onEvent });
    expect(onEvent).toHaveBeenCalledWith("token", "hello");
  });

  it("lève une erreur si la réponse n'est pas ok", async () => {
    fetchMock.mockResolvedValue(
      sseResponse([], { ok: false, status: 500, statusText: "Server Error" })
    );
    // text() renvoie "" -> message basé sur le statut
    await expect(readSseStream({ url: "/x", body: {}, onEvent: vi.fn() })).rejects.toThrow();
  });

  it("n'émet jamais de header Authorization et inclut les cookies", async () => {
    fetchMock.mockResolvedValue(sseResponse(["event: done\ndata: {}\n\n"]));
    await readSseStream({ url: "/x", body: {}, onEvent: vi.fn() });
    const init = fetchMock.mock.calls.at(-1)![1] as RequestInit;
    expect((init.headers as Headers).get("authorization")).toBeNull();
    expect(init.credentials).toBe("include");
  });

  it("ajoute X-CSRFToken sur le POST streaming quand le cookie existe", async () => {
    vi.spyOn(auth, "getCsrfToken").mockReturnValue("csrf-2");
    fetchMock.mockResolvedValue(sseResponse(["event: done\ndata: {}\n\n"]));
    await readSseStream({ url: "/x", body: {}, onEvent: vi.fn() });
    const init = fetchMock.mock.calls.at(-1)![1] as RequestInit;
    expect((init.headers as Headers).get("x-csrftoken")).toBe("csrf-2");
  });
});
