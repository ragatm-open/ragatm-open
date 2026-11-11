import { describe, expect, it } from "vitest";

import { UPLOAD_ACCEPT, uploadAccept, uploadFormatsLabel } from "@/app/documents/_lib/types";

/** `.dxf` proposé au dépôt uniquement si le serveur annonce `dxf`. */
describe("ouverture contrôlée de l'upload DXF", () => {
  it(".dxf est ABSENT de la liste statique", () => {
    expect(UPLOAD_ACCEPT.split(",")).not.toContain(".dxf");
  });

  it(".dxf est ajouté quand le backend annonce le format", () => {
    expect(uploadAccept(null, ["xml", "dxf"]).split(",")).toContain(".dxf");
    expect(uploadFormatsLabel(null, ["dxf"])).toMatch(/DXF \(\.dxf\)/);
  });

  it(".dxf reste absent sans annonce ; xml et dxf sont indépendants", () => {
    expect(uploadAccept(null, ["xml"]).split(",")).not.toContain(".dxf");
    const both = uploadAccept(null, ["xml", "dxf"]).split(",");
    expect(both).toContain(".xml");
    expect(both).toContain(".dxf");
  });
});
