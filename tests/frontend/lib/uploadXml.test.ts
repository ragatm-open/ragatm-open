import { describe, expect, it } from "vitest";

import { UPLOAD_ACCEPT, uploadAccept, uploadFormatsLabel } from "@/app/documents/_lib/types";

/**
 * `.xml` n'est proposé au dépôt que si CET environnement l'annonce
 * (`ingest.formats` contient "xml"). Il n'entre jamais dans la constante statique :
 * le flag serveur est fermé par défaut.
 */
describe("ouverture contrôlée de l'upload XML", () => {
  it(".xml est ABSENT de la liste statique", () => {
    expect(UPLOAD_ACCEPT.split(",")).not.toContain(".xml");
  });

  it(".xml est ajouté quand le backend annonce le format", () => {
    expect(uploadAccept(null, ["pdf", "xml"]).split(",")).toContain(".xml");
    expect(uploadFormatsLabel(null, ["pdf", "xml"])).toMatch(/XML \(\.xml\)/);
  });

  it(".xml reste absent sans annonce, et sans casser .msg", () => {
    expect(uploadAccept(null, ["pdf"]).split(",")).not.toContain(".xml");
    expect(uploadAccept(null).split(",")).not.toContain(".xml");
    const msg = { format: "msg", status: "available", body_ingestion: true, attachment_ingestion: false, inline_image_ocr: false } as const;
    const both = uploadAccept(msg, ["xml", "msg"]).split(",");
    expect(both).toContain(".msg");
    expect(both).toContain(".xml");
  });
});
