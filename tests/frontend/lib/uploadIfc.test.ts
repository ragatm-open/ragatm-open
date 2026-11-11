import { describe, expect, it } from "vitest";

import { UPLOAD_ACCEPT, uploadAccept, uploadFormatsLabel } from "@/app/documents/_lib/types";

/** `.ifc` proposé au dépôt uniquement si le serveur annonce `ifc`. */
describe("ouverture contrôlée de l'upload IFC", () => {
  it(".ifc est ABSENT de la liste statique", () => {
    expect(UPLOAD_ACCEPT.split(",")).not.toContain(".ifc");
  });

  it(".ifc est ajouté quand le backend annonce le format", () => {
    expect(uploadAccept(null, ["ifc"]).split(",")).toContain(".ifc");
    expect(uploadFormatsLabel(null, ["ifc"])).toMatch(/IFC \(\.ifc\)/);
  });

  it(".ifc reste absent sans annonce ; les formats structurés sont indépendants", () => {
    expect(uploadAccept(null, ["xml", "dxf"]).split(",")).not.toContain(".ifc");
    const all = uploadAccept(null, ["xml", "dxf", "ifc"]).split(",");
    expect(all).toContain(".xml");
    expect(all).toContain(".dxf");
    expect(all).toContain(".ifc");
  });
});
