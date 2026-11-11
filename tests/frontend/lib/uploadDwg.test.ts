import { describe, expect, it } from "vitest";

import { UPLOAD_ACCEPT, uploadAccept, uploadFormatsLabel } from "@/app/documents/_lib/types";

/** `.dwg` proposé au dépôt uniquement si le serveur annonce `dwg`. */
describe("ouverture contrôlée de l'upload DWG", () => {
  it(".dwg est ABSENT de la liste statique", () => {
    expect(UPLOAD_ACCEPT.split(",")).not.toContain(".dwg");
  });

  it(".dwg est ajouté quand le backend annonce le format", () => {
    expect(uploadAccept(null, ["dwg"]).split(",")).toContain(".dwg");
    expect(uploadFormatsLabel(null, ["dwg"])).toMatch(/DWG \(\.dwg\)/);
  });

  it(".dwg reste absent sans annonce ; les formats structurés sont indépendants", () => {
    expect(uploadAccept(null, ["xml", "dxf", "ifc"]).split(",")).not.toContain(".dwg");
    const all = uploadAccept(null, ["xml", "dxf", "ifc", "dwg"]).split(",");
    for (const ext of [".xml", ".dxf", ".ifc", ".dwg"]) expect(all).toContain(ext);
  });

  it("aucun format CAO n'entre dans la liste statique", () => {
    const statics = UPLOAD_ACCEPT.split(",");
    for (const ext of [".dwg", ".dxf", ".ifc"]) expect(statics).not.toContain(ext);
  });
});
