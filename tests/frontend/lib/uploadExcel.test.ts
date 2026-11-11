import { describe, it, expect } from "vitest";
import {
  UPLOAD_ACCEPT,
  UPLOAD_FORMATS_LABEL,
  FILE_COLORS,
  humanError,
} from "@/app/documents/_lib/types";
import {
  SUPPORTED_EXTENSIONS,
  isSupportedFilename,
  buildFolderPlan,
} from "@/app/documents/_lib/folderImport";

describe("ouverture contrôlée de l'upload Excel (E4)", () => {
  it(".xlsx est proposé à l'upload", () => {
    expect(UPLOAD_ACCEPT).toContain(".xlsx");
    expect(UPLOAD_FORMATS_LABEL).toMatch(/Excel \(\.xlsx\)/);
  });

  it("les autres formats tableur restent ABSENTS de l'interface", () => {
    // Le backend les refuse par CONTENU de toute façon — autant ne pas les proposer.
    expect(UPLOAD_ACCEPT).not.toContain(".xlsm");
    expect(UPLOAD_ACCEPT).not.toContain(".ods");
    expect(UPLOAD_ACCEPT).not.toContain(".xlsb");
    // `.xls` : attention, `.xlsx` le contient comme sous-chaîne. On teste l'entrée exacte.
    expect(UPLOAD_ACCEPT.split(",")).not.toContain(".xls");
    expect(UPLOAD_FORMATS_LABEL).not.toMatch(/\.xlsm|\.ods|\.xlsb/);
  });

  it("le classeur a sa pastille de type", () => {
    expect(FILE_COLORS.XLSX).toBeTruthy();
  });
});

describe("import de dossier — dérive bien d'UPLOAD_ACCEPT (source unique)", () => {
  it(".xlsx est accepté", () => {
    expect(SUPPORTED_EXTENSIONS).toContain("xlsx"); // sans point (cf. folderImport)
    expect(isSupportedFilename("Budget_2026.xlsx")).toBe(true);
    expect(isSupportedFilename("BUDGET.XLSX")).toBe(true);
  });

  it(".xlsm, .xls, .ods, .xlsb sont filtrés", () => {
    expect(isSupportedFilename("macros.xlsm")).toBe(false);
    expect(isSupportedFilename("vieux.xls")).toBe(false);
    expect(isSupportedFilename("libre.ods")).toBe(false);
    expect(isSupportedFilename("binaire.xlsb")).toBe(false);
  });

  it("un dossier mixte : le classeur passe, les autres tableurs sont écartés", () => {
    const files = [
      { name: "Budget_2026.xlsx", size: 1000, webkitRelativePath: "Finance/Budget_2026.xlsx" },
      { name: "macros.xlsm", size: 1000, webkitRelativePath: "Finance/macros.xlsm" },
      { name: "vieux.xls", size: 1000, webkitRelativePath: "Finance/vieux.xls" },
      { name: "libre.ods", size: 1000, webkitRelativePath: "Finance/libre.ods" },
      { name: "note.pdf", size: 1000, webkitRelativePath: "Finance/note.pdf" },
    ] as unknown as File[];

    const plan = buildFolderPlan(files, { maxSizeBytes: 25 * 1024 * 1024 });
    const kept = plan.compatible.map((f) => f.file.name);
    const skipped = plan.skipped.map((f) => f.name);

    expect(kept).toContain("Budget_2026.xlsx");
    expect(kept).toContain("note.pdf");
    expect(skipped).toEqual(
      expect.arrayContaining(["macros.xlsm", "vieux.xls", "libre.ods"]),
    );
  });
});

describe("messages d'erreur Excel (E2/E4)", () => {
  it("corpus métier fermé -> message explicite, jamais générique", () => {
    const msg = humanError("excel_not_supported_here");
    expect(msg).toMatch(/corpus/i);
    expect(msg).toMatch(/privé|CSV/i);
    expect(msg).not.toMatch(/non reconnu/i);
  });

  it("classeur trop volumineux", () => {
    expect(humanError("excel_shared_strings_too_large")).toMatch(/volumineux/i);
    expect(humanError("excel_too_many_cells")).toMatch(/volumineux/i);
  });

  it("classeur corrompu", () => {
    expect(humanError("excel_corrupted")).toMatch(/illisible|corrompu/i);
  });

  it("macros et formats hérités gardent leurs messages (non-régression E0)", () => {
    expect(humanError("macro_enabled_workbook_refused")).toMatch(/macros/i);
    expect(humanError("legacy_office_format")).toMatch(/\.xls/i);
    expect(humanError("unsupported_spreadsheet_format")).toMatch(/OpenDocument|\.ods/i);
  });
});
