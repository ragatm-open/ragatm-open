import { describe, expect, it } from "vitest";

import {
  FORMAT_CATALOG,
  UPLOAD_ACCEPT,
  uploadAccept,
  uploadFormatsLabel,
} from "@/app/documents/_lib/types";
import {
  folderFormatCatalog,
  isSupportedFilename,
  UNKNOWN_FORMAT_KEY,
} from "@/app/documents/_lib/folderImport";

const exts = (accept: string) =>
  accept.split(",").map((e) => e.trim().replace(/^\./, "").toLowerCase()).filter(Boolean);

/**
 * Le contrat de ce lot : `ingest.formats` est la SEULE autorité d'admission, pour
 * TOUS les formats — y compris les seize historiquement codés en dur. Un format
 * fermé côté serveur disparaît de l'`accept` sans qu'une ligne de frontend bouge,
 * et un format ouvert y apparaît sans patch dédié.
 */
describe("accept piloté par les capacités serveur (tous formats)", () => {
  it("PDF seul annoncé => PDF seul proposé", () => {
    expect(exts(uploadAccept(null, ["pdf"]))).toEqual(["pdf"]);
  });

  it("PDF + MSG => .pdf et .msg, rien d'autre", () => {
    expect(exts(uploadAccept(null, ["pdf", "msg"])).sort()).toEqual(["msg", "pdf"]);
  });

  it("MSG non annoncé => .msg absent", () => {
    expect(exts(uploadAccept(null, ["pdf", "docx"]))).not.toContain("msg");
  });

  it("IFC annoncé => .ifc présent", () => {
    expect(exts(uploadAccept(null, ["pdf", "ifc"]))).toContain("ifc");
  });

  it("Excel FERMÉ côté serveur => .xlsx disparaît (socle historique compris)", () => {
    // C'est la divergence architecturale que ce lot ferme : avant, `.xlsx` était
    // proposé même flag fermé, et le backend refusait ensuite fichier par fichier.
    const sansExcel = exts(uploadAccept(null, ["pdf", "docx", "csv"]));
    expect(sansExcel).not.toContain("xlsx");
    expect(exts(uploadAccept(null, ["pdf", "excel"]))).toContain("xlsx");
  });

  it("DWG annoncé => .dwg présent ; non annoncé => absent", () => {
    expect(exts(uploadAccept(null, ["dwg"]))).toContain("dwg");
    expect(exts(uploadAccept(null, ["pdf"]))).not.toContain("dwg");
  });

  it("aucun format annoncé => aucune extension proposée", () => {
    expect(exts(uploadAccept(null, []))).toEqual([]);
  });

  it("capacités non chargées (null) => repli sur le socle statique", () => {
    expect(uploadAccept(null, null)).toBe(UPLOAD_ACCEPT);
    expect(uploadAccept(null, undefined)).toBe(UPLOAD_ACCEPT);
  });
});

describe("alias d'extensions préservés par la généralisation", () => {
  it("html => .html ET .htm", () => {
    const e = exts(uploadAccept(null, ["html"]));
    expect(e).toContain("html");
    expect(e).toContain("htm");
  });

  it("markdown => .md ET .markdown", () => {
    const e = exts(uploadAccept(null, ["markdown"]));
    expect(e).toContain("md");
    expect(e).toContain("markdown");
  });

  it("excel => .xlsx seul (jamais .xls ni .xlsm)", () => {
    const e = exts(uploadAccept(null, ["excel"]));
    expect(e).toEqual(["xlsx"]);
  });

  it("pptx => .pptx seul (jamais .ppt ni .pptm)", () => {
    expect(exts(uploadAccept(null, ["pptx"]))).toEqual(["pptx"]);
  });
});

describe("famille image : les sous-formats viennent du backend", () => {
  it("jpeg + png annoncés => .jpg .jpeg .png, sans .tif ni .webp", () => {
    const e = exts(uploadAccept(null, ["image"], ["jpeg", "png"]));
    expect(e.sort()).toEqual(["jpeg", "jpg", "png"]);
  });

  it("tiff annoncé => .tif ET .tiff", () => {
    const e = exts(uploadAccept(null, ["image"], ["tiff"]));
    expect(e.sort()).toEqual(["tif", "tiff"]);
  });

  it("webp retiré de l'allowlist serveur => .webp disparaît", () => {
    expect(exts(uploadAccept(null, ["image"], ["jpeg", "png", "tiff"]))).not.toContain("webp");
    expect(exts(uploadAccept(null, ["image"], ["jpeg", "webp"]))).toContain("webp");
  });

  it("famille image annoncée SANS sous-format => aucune extension image", () => {
    // Cohérent avec le backend : porte image fermée => `supported_image_formats()` vide.
    expect(exts(uploadAccept(null, ["image"], []))).toEqual([]);
  });
});

describe("robustesse du catalogue", () => {
  it("une clé annoncée hors catalogue reste déposable (jamais silencieusement perdue)", () => {
    expect(exts(uploadAccept(null, ["pdf", "formatinedit"]))).toContain("formatinedit");
  });

  it("le catalogue ne contient aucun doublon de clé", () => {
    const keys = FORMAT_CATALOG.map((f) => f.key);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it("le libellé d'aide suit les mêmes capacités que l'accept", () => {
    const label = uploadFormatsLabel(null, ["pdf", "ifc"]);
    expect(label).toMatch(/PDF/);
    expect(label).toMatch(/IFC/);
    expect(label).not.toMatch(/Excel/);
  });
});

describe("l'import de dossier consomme les mêmes capacités", () => {
  it("un format ouvert au serveur devient importable par le dossier, sans patch", () => {
    const cat = folderFormatCatalog(uploadAccept(null, ["pdf", "msg"]));
    expect(isSupportedFilename("note.msg", cat)).toBe(true);
    expect(isSupportedFilename("rapport.pdf", cat)).toBe(true);
    expect(isSupportedFilename("plan.ifc", cat)).toBe(false);
  });

  it("un fichier sans extension est admis, sous un format DISTINCT", () => {
    const cat = folderFormatCatalog(uploadAccept(null, ["pdf"]));
    expect(isSupportedFilename("PLAN", cat)).toBe(true);
    expect(cat.formats.some((f) => f.key === UNKNOWN_FORMAT_KEY)).toBe(true);
    // Il n'est JAMAIS présenté comme un PDF avant détection serveur.
    const unknown = cat.formats.find((f) => f.key === UNKNOWN_FORMAT_KEY);
    expect(unknown?.key).not.toBe("pdf");
  });

  it("serveur n'annonçant rien => même les fichiers sans extension sont écartés", () => {
    const cat = folderFormatCatalog(uploadAccept(null, []));
    expect(isSupportedFilename("PLAN", cat)).toBe(false);
    expect(cat.formats).toEqual([]);
  });
});
