import { describe, expect, it } from "vitest";

import {
  FILE_COLORS,
  UPLOAD_ACCEPT,
  UPLOAD_FORMATS_LABEL,
  humanError,
} from "@/app/documents/_lib/types";
import {
  SUPPORTED_EXTENSIONS,
  buildFolderPlan,
  isSupportedFilename,
} from "@/app/documents/_lib/folderImport";
import { fileTypeLabel } from "@/lib/format";

describe("ouverture contrôlée de l'upload PowerPoint (P1)", () => {
  it(".pptx est proposé à l'upload", () => {
    expect(UPLOAD_ACCEPT).toContain(".pptx");
    expect(UPLOAD_FORMATS_LABEL).toMatch(/PowerPoint \(\.pptx\)/);
  });

  it("les autres formats de présentation restent ABSENTS de l'interface", () => {
    // Le backend les refuse par CONTENU de toute façon — autant ne pas les proposer.
    expect(UPLOAD_ACCEPT).not.toContain(".pptm");
    expect(UPLOAD_ACCEPT).not.toContain(".odp");
    // `.ppt` : attention, `.pptx` le contient comme sous-chaîne. On teste l'entrée exacte.
    expect(UPLOAD_ACCEPT.split(",")).not.toContain(".ppt");
    expect(UPLOAD_FORMATS_LABEL).not.toMatch(/\.pptm|\.odp/);
  });

  it("la présentation a sa pastille de type", () => {
    expect(FILE_COLORS.PPTX).toBeTruthy();
  });

  it("le libellé de pastille est dérivé du nom de fichier", () => {
    expect(fileTypeLabel("architecture.pptx")).toBe("PPTX");
  });

  it("les pastilles existantes restent intactes", () => {
    expect(FILE_COLORS.PDF).toBeTruthy();
    expect(FILE_COLORS.DOCX).toBeTruthy();
    expect(FILE_COLORS.XLSX).toBeTruthy();
  });
});

describe("import de dossier — dérive bien d'UPLOAD_ACCEPT (source unique)", () => {
  it(".pptx est accepté", () => {
    expect(SUPPORTED_EXTENSIONS).toContain("pptx"); // sans point (cf. folderImport)
    expect(isSupportedFilename("Architecture.pptx")).toBe(true);
    expect(isSupportedFilename("ARCHITECTURE.PPTX")).toBe(true);
  });

  it(".pptm, .ppt, .odp sont filtrés", () => {
    expect(isSupportedFilename("macros.pptm")).toBe(false);
    expect(isSupportedFilename("vieux.ppt")).toBe(false);
    expect(isSupportedFilename("libre.odp")).toBe(false);
  });

  it("un dossier mixte : la présentation passe, les autres formats sont écartés", () => {
    const files = [
      { name: "Archi.pptx", size: 1000, webkitRelativePath: "Comités/Archi.pptx" },
      { name: "macros.pptm", size: 1000, webkitRelativePath: "Comités/macros.pptm" },
      { name: "vieux.ppt", size: 1000, webkitRelativePath: "Comités/vieux.ppt" },
      { name: "libre.odp", size: 1000, webkitRelativePath: "Comités/libre.odp" },
      { name: "note.pdf", size: 1000, webkitRelativePath: "Comités/note.pdf" },
    ] as unknown as File[];

    const plan = buildFolderPlan(files, { maxSizeBytes: 25 * 1024 * 1024 });
    const kept = plan.compatible.map((f) => f.file.name);
    const skipped = plan.skipped.map((f) => f.name);

    expect(kept).toContain("Archi.pptx");
    expect(kept).toContain("note.pdf");
    expect(skipped).toContain("macros.pptm");
    expect(skipped).toContain("vieux.ppt");
    expect(skipped).toContain("libre.odp");
  });

  it("les métadonnées d'import sont conservées pour un .pptx", () => {
    const files = [
      { name: "Archi.pptx", size: 1000, webkitRelativePath: "Comités/2026/Archi.pptx" },
    ] as unknown as File[];
    const plan = buildFolderPlan(files, { maxSizeBytes: 25 * 1024 * 1024 });
    expect(plan.compatible[0].sourceFolder).toBe("Comités");
    expect(plan.compatible[0].relativePath).toBe("2026/Archi.pptx");
  });
});

describe("messages d'erreur PowerPoint (P1)", () => {
  it("un .pptm à macros est expliqué, pas juste refusé", () => {
    const msg = humanError("macro_enabled_presentation_refused");
    expect(msg).toMatch(/macros/i);
    expect(msg).toMatch(/\.pptx/);
  });

  it("un .odp reçoit la consigne qui le concerne", () => {
    const msg = humanError("unsupported_presentation_format");
    expect(msg).toMatch(/OpenDocument|\.odp/i);
    expect(msg).toMatch(/\.pptx/);
    // Régression visée : ne plus parler d'archive pour une présentation.
    expect(msg).not.toMatch(/archive/i);
  });

  it("une présentation chiffrée est nommée comme telle", () => {
    expect(humanError("encrypted_pptx")).toMatch(/protégée|chiffrée/i);
  });

  it("une archive corrompue est nommée comme telle", () => {
    expect(humanError("invalid_pptx_archive")).toMatch(/illisible|corrompue/i);
  });

  it("un dépassement de limite est expliqué", () => {
    expect(humanError("pptx_limit_exceeded")).toMatch(/volumineuse/i);
  });

  it("le refus dans le corpus métier oriente vers le PDF", () => {
    expect(humanError("pptx_not_supported_here")).toMatch(/PDF/);
  });

  it("le flag fermé est expliqué", () => {
    expect(humanError("pptx_disabled")).toMatch(/désactivée/i);
  });

  it("le message Office générique ne prétend plus que PowerPoint est non supporté", () => {
    // Avant P1 : « Format Office non supporté (PowerPoint, ou Excel désactivé) ».
    expect(humanError("unsupported_office_format")).not.toMatch(/PowerPoint/);
  });

  it("le message des formats hérités mentionne le .pptx comme cible", () => {
    expect(humanError("legacy_office_format")).toMatch(/\.pptx/);
  });
});

describe("non-régression Excel", () => {
  it("le classeur reste proposé et ses messages inchangés", () => {
    expect(UPLOAD_ACCEPT).toContain(".xlsx");
    expect(UPLOAD_FORMATS_LABEL).toMatch(/Excel \(\.xlsx\)/);
    expect(humanError("macro_enabled_workbook_refused")).toMatch(/macros/i);
    expect(humanError("excel_not_supported_here")).toMatch(/CSV/);
  });
});
