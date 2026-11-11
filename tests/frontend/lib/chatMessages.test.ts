import { describe, expect, it } from "vitest";

import {
  mapServerMessage,
  mapServerMessages,
  messageKey,
  reconcileChatMessages,
  type WireChatMessage,
} from "@/app/chat/_lib/chatMessages";
import type { Msg } from "@/app/chat/_lib/types";

function wire(over: Partial<WireChatMessage> = {}): WireChatMessage {
  return {
    id: "m-1",
    role: "assistant",
    content: "Résumé du document",
    created_at: "2026-07-19T10:00:00Z",
    request_id: "rq-1",
    kind: "document_summary",
    status: "completed",
    document_id: "doc-1",
    summary_id: "sum-1",
    ...over,
  };
}

describe("mapServerMessage — aucun champ perdu à la relecture", () => {
  it("conserve kind, status, summary_id et document_id", () => {
    const m = mapServerMessage(wire());
    expect(m.category).toBe("document_summary");
    expect(m.status).toBe("completed");
    expect(m.summary_id).toBe("sum-1");
    expect(m.document_id).toBe("doc-1");
    expect(m.text).toBe("Résumé du document");
  });

  it("conserve l'identité (id + request_id) nécessaire à la réconciliation", () => {
    const m = mapServerMessage(wire());
    expect(m.id).toBe("m-1");
    expect(m.request_id).toBe("rq-1");
  });

  it("conserve analysis_id — sans lui la carte d'analyse disparaît du fil", () => {
    const m = mapServerMessage(wire({ analysis_id: "an-1" }));
    expect(m.analysis_id).toBe("an-1");
  });

  it("rend lisible un message ancien, sans aucun des nouveaux champs", () => {
    const m = mapServerMessage({
      id: "old-1",
      role: "assistant",
      content: "Réponse d'avant la migration",
    });
    expect(m.text).toBe("Réponse d'avant la migration");
    expect(m.category).toBeNull();
    expect(m.status).toBeNull();
  });

  it("dégrade un kind ou un statut inconnu plutôt que de casser le rendu", () => {
    const m = mapServerMessage(wire({ kind: "some_future_kind", status: "archived" }));
    expect(m.category).toBeNull();
    expect(m.status).toBeNull();
    expect(m.text).toBe("Résumé du document");
  });

  it("mappe une liste vide ou absente sans exploser", () => {
    expect(mapServerMessages(null)).toEqual([]);
    expect(mapServerMessages(undefined)).toEqual([]);
  });
});

describe("reconcileChatMessages — le serveur fait autorité", () => {
  const optimistic: Msg = {
    role: "assistant",
    text: "",
    request_id: "rq-1",
    category: "document_summary",
    status: "pending",
  };

  it("remplace le message optimiste par la version serveur, sans doublon", () => {
    const server = [mapServerMessage(wire())];
    const out = reconcileChatMessages([optimistic], server);

    expect(out).toHaveLength(1);
    expect(out[0].id).toBe("m-1");
    expect(out[0].status).toBe("completed");
    expect(out[0].text).toBe("Résumé du document");
  });

  it("ne duplique pas quand le serveur connaît déjà le message par son id", () => {
    const local: Msg = { ...optimistic, id: "m-1", status: "completed", text: "Résumé du document" };
    const out = reconcileChatMessages([local], [mapServerMessage(wire())]);
    expect(out).toHaveLength(1);
  });

  it("conserve un message local dont la requête est encore en vol", () => {
    const inFlight: Msg = { ...optimistic, request_id: "rq-2" };
    const out = reconcileChatMessages([inFlight], [mapServerMessage(wire())]);

    expect(out).toHaveLength(2);
    expect(out[0].id).toBe("m-1"); // serveur d'abord
    expect(out[1].request_id).toBe("rq-2"); // local en attente, en queue
  });

  it("distingue la demande de la réponse pour un même request_id", () => {
    const localUser: Msg = { role: "user", text: "Résume", request_id: "rq-1" };
    const out = reconcileChatMessages([localUser, optimistic], [mapServerMessage(wire())]);

    // La réponse assistant est réconciliée ; la demande utilisateur, que le
    // serveur n'a pas encore renvoyée dans cet extrait, reste affichée.
    expect(out).toHaveLength(2);
    expect(out.filter((m) => m.role === "assistant")).toHaveLength(1);
  });

  it("abandonne un message local non identifiable — il n'a jamais été persisté", () => {
    const ghost: Msg = { role: "assistant", text: "jamais persisté" };
    const out = reconcileChatMessages([ghost], [mapServerMessage(wire())]);
    expect(out).toHaveLength(1);
    expect(out[0].id).toBe("m-1");
  });

  it("conserve l'historique complet quand rien n'est en vol localement", () => {
    const server = [
      mapServerMessage(wire({ id: "m-1", role: "user", content: "Résume", kind: "document_summary" })),
      mapServerMessage(wire({ id: "m-2" })),
      mapServerMessage(wire({ id: "m-3", request_id: "rq-2", content: "Réponse suivante", kind: "" })),
    ];
    const out = reconcileChatMessages([], server);
    expect(out.map((m) => m.id)).toEqual(["m-1", "m-2", "m-3"]);
  });

  it("garde le résumé visible après une nouvelle question", () => {
    // Le fil rechargé contient le résumé PUIS la question suivante : c'est
    // exactement le scénario où le résumé disparaissait auparavant.
    const server = [
      mapServerMessage(wire({ id: "m-1", content: "Le résumé" })),
      mapServerMessage(wire({ id: "m-2", request_id: "rq-2", content: "Réponse", kind: "" })),
    ];
    const out = reconcileChatMessages([], server);
    expect(out.map((m) => m.text)).toEqual(["Le résumé", "Réponse"]);
  });

  it("reconstruit le résumé depuis le seul historique, sans état local", () => {
    // Simule un refresh : plus aucun message local.
    const out = reconcileChatMessages([], [mapServerMessage(wire())]);
    expect(out[0].category).toBe("document_summary");
    expect(out[0].status).toBe("completed");
    expect(out[0].text).toBe("Résumé du document");
  });

  it("préserve un message pending renvoyé par le serveur", () => {
    const out = reconcileChatMessages(
      [optimistic],
      [mapServerMessage(wire({ status: "pending", content: "⏳ Résumé en cours de génération…" }))]
    );
    expect(out).toHaveLength(1);
    expect(out[0].status).toBe("pending");
  });
});

describe("messageKey — clé React stable", () => {
  it("préfère l'identifiant serveur", () => {
    expect(messageKey({ role: "assistant", text: "", id: "m-1", request_id: "rq-1" }, 3)).toBe("m-1");
  });

  it("retombe sur request_id + rôle tant que le serveur n'a pas répondu", () => {
    expect(messageKey({ role: "assistant", text: "", request_id: "rq-1" }, 3)).toBe("rq-1:assistant");
    expect(messageKey({ role: "user", text: "", request_id: "rq-1" }, 2)).toBe("rq-1:user");
  });

  it("n'utilise l'index qu'en dernier recours", () => {
    expect(messageKey({ role: "assistant", text: "" }, 5)).toBe("idx:5");
  });

  it("donne des clés distinctes à la demande et à la réponse d'un même résumé", () => {
    const u = messageKey({ role: "user", text: "", request_id: "rq-1" }, 0);
    const a = messageKey({ role: "assistant", text: "", request_id: "rq-1" }, 1);
    expect(u).not.toBe(a);
  });
});
