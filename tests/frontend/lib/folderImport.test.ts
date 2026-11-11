import { describe, it, expect } from "vitest";
import {
  parseWebkitPath,
  isSupportedFilename,
  buildFolderPlan,
  buildUploadForm,
  humanBytes,
  defaultSelectedFormats,
  extensionsOfFormats,
  formatOfFilename,
  summarizeFolderFormats,
  IMPORT_FORMATS,
  SUPPORTED_EXTENSIONS,
  folderFormatCatalog,
  UNKNOWN_FORMAT_KEY,
  MAX_FOLDER_FILES,
  MAX_FOLDER_TOTAL_MB,
  FOLDER_UPLOAD_CONCURRENCY,
} from "@/app/documents/_lib/folderImport";
import { UPLOAD_ACCEPT, uploadAccept } from "@/app/documents/_lib/types";

/** Faux File avec webkitRelativePath (non défini par le constructeur jsdom). */
function fakeFile(relPath: string, size: number, name?: string): File {
  const base = name ?? relPath.split("/").pop() ?? relPath;
  const f = new File([new Uint8Array(Math.max(0, size))], base, { type: "" });
  Object.defineProperty(f, "webkitRelativePath", { value: relPath, configurable: true });
  return f;
}

/** Faux File dont seule la TAILLE annoncée compte : aucun octet alloué. */
function sizedFile(relPath: string, size: number): File {
  const f = fakeFile(relPath, 0);
  Object.defineProperty(f, "size", { value: size, configurable: true });
  return f;
}

describe("parseWebkitPath", () => {
  it("sépare le dossier racine et le chemin relatif", () => {
    expect(parseWebkitPath("Ressources humaines/procédures/congés.pdf", "congés.pdf")).toEqual({
      sourceFolder: "Ressources humaines",
      relativePath: "procédures/congés.pdf",
    });
  });
  it("gère un fichier à la racine du dossier sélectionné", () => {
    expect(parseWebkitPath("root/file.pdf", "file.pdf")).toEqual({
      sourceFolder: "root",
      relativePath: "file.pdf",
    });
  });
  it("normalise les backslashes et le / initial", () => {
    expect(parseWebkitPath("\\root\\sub\\a.txt", "a.txt")).toEqual({
      sourceFolder: "root",
      relativePath: "sub/a.txt",
    });
  });
});

describe("isSupportedFilename (source unique UPLOAD_ACCEPT)", () => {
  it("accepte les formats du pipeline", () => {
    for (const n of ["a.pdf", "b.docx", "c.md", "d.csv", "e.html", "f.txt", "g.png", "h.webp"]) {
      expect(isSupportedFilename(n)).toBe(true);
    }
  });
  it("rejette les formats non supportés", () => {
    // NB : `.xlsx` est ACCEPTÉ depuis le lot E4 (cf. uploadExcel.test.ts) et
    // `.pptx` depuis une version antérieure (cf. uploadPptx.test.ts). Les variantes à macros et
    // les formats hérités, eux, restent filtrés.
    for (const n of ["a.exe", "b.zip", "c.xlsm", "d.pptm", "f.ppt", "g.odp"]) {
      expect(isSupportedFilename(n)).toBe(false);
    }
  });
  it("un fichier SANS extension est admis au serveur, jamais refusé d'avance", () => {
    // Le frontend ne sait pas ce que contient `PLAN` ; le backend le sait, par
    // signature. Refuser ici, c'est décider à sa place — et perdre un PDF légitime.
    expect(isSupportedFilename("PLAN")).toBe(true);
    expect(isSupportedFilename("e")).toBe(true);
  });
  it("dérive les extensions d'UPLOAD_ACCEPT", () => {
    expect(SUPPORTED_EXTENSIONS.has("pdf")).toBe(true);
    expect(SUPPORTED_EXTENSIONS.has("exe")).toBe(false);
  });
});

describe("buildFolderPlan", () => {
  const MAX = 25 * 1024 * 1024;

  it("filtre non-supportés / vides / trop gros, conserve les chemins relatifs", () => {
    const files = [
      fakeFile("HR/root.pdf", 1000),
      fakeFile("HR/policies/leave.docx", 2000),
      fakeFile("HR/policies/remote.md", 500),
      fakeFile("HR/images/diagram.png", 3000),
      fakeFile("HR/ignored/binary.exe", 4000), // non supporté
      fakeFile("HR/big.pdf", MAX + 1), // trop gros
      fakeFile("HR/empty.txt", 0), // vide
    ];
    const plan = buildFolderPlan(files, { maxSizeBytes: MAX });

    expect(plan.sourceFolder).toBe("HR");
    expect(plan.totalFound).toBe(7);
    expect(plan.compatible.map((c) => c.relativePath).sort()).toEqual([
      "images/diagram.png",
      "policies/leave.docx",
      "policies/remote.md",
      "root.pdf",
    ]);
    expect(plan.skipped.filter((s) => s.reason === "unsupported")).toHaveLength(1);
    expect(plan.skipped.filter((s) => s.reason === "too_large")).toHaveLength(1);
    expect(plan.skipped.filter((s) => s.reason === "empty")).toHaveLength(1);
    expect(plan.totalBytes).toBe(1000 + 2000 + 500 + 3000);
    expect(plan.tooManyFiles).toBe(false);
    expect(plan.tooLarge).toBe(false);
    // Chaque fichier compatible conserve sa provenance.
    const leave = plan.compatible.find((c) => c.relativePath === "policies/leave.docx")!;
    expect(leave.sourceFolder).toBe("HR");
  });

  it("la borne de nombre vaut 1 000 ; les autres bornes du lot sont inchangées", () => {
    expect(MAX_FOLDER_FILES).toBe(1000);
    expect(MAX_FOLDER_TOTAL_MB).toBe(1024);
    expect(FOLDER_UPLOAD_CONCURRENCY).toBe(4);
  });

  it.each([1, 500, 501, 999, 1000])("accepte un lot de %i fichiers", (n) => {
    const files = Array.from({ length: n }, (_, i) => fakeFile(`HR/f${i}.txt`, 10));
    const plan = buildFolderPlan(files, { maxSizeBytes: 10_000 });
    expect(plan.compatible).toHaveLength(n);
    expect(plan.tooManyFiles).toBe(false);
    expect(plan.tooLarge).toBe(false);
  });

  it("1 000 fichiers juste sous 1 Gio au total sont acceptés", () => {
    // Taille simulée (pas de 1 Gio en mémoire) : seul `size` est lu par le plan.
    const per = Math.floor((MAX_FOLDER_TOTAL_MB * 1024 * 1024) / 1000);
    const files = Array.from({ length: 1000 }, (_, i) => sizedFile(`HR/f${i}.pdf`, per));
    const plan = buildFolderPlan(files, { maxSizeBytes: 50 * 1024 * 1024 });
    expect(plan.compatible).toHaveLength(1000);
    expect(plan.totalBytes).toBeLessThanOrEqual(MAX_FOLDER_TOTAL_MB * 1024 * 1024);
    expect(plan.tooManyFiles).toBe(false);
    expect(plan.tooLarge).toBe(false);
  });

  it("un lot au-delà de 1 Gio reste refusé, même sous 1 000 fichiers", () => {
    const per = Math.floor((MAX_FOLDER_TOTAL_MB * 1024 * 1024) / 1000) + 2048;
    const files = Array.from({ length: 1000 }, (_, i) => sizedFile(`HR/f${i}.pdf`, per));
    const plan = buildFolderPlan(files, { maxSizeBytes: 50 * 1024 * 1024 });
    expect(plan.tooManyFiles).toBe(false);
    expect(plan.tooLarge).toBe(true);
  });

  it("formats mixtes : seuls les compatibles comptent pour la borne de 1 000", () => {
    // 1 000 compatibles + des fichiers écartés (format, verrou Office, vide) : la
    // borne porte sur les COMPATIBLES, pas sur ce que le dossier contient.
    const files = [
      ...Array.from({ length: 1000 }, (_, i) => fakeFile(`HR/f${i}.${i % 2 ? "pdf" : "docx"}`, 10)),
      fakeFile("HR/setup.exe", 10),
      fakeFile("HR/~$budget.xlsx", 10),
      fakeFile("HR/vide.txt", 0),
    ];
    const plan = buildFolderPlan(files, { maxSizeBytes: 10_000 });
    expect(plan.compatible).toHaveLength(1000);
    expect(plan.tooManyFiles).toBe(false);
    expect(plan.skipped.map((s) => s.reason).sort()).toEqual(["empty", "office_temporary_file", "unsupported"]);
  });

  it("signale un lot avec trop de fichiers (1 001)", () => {
    const files = Array.from({ length: 1001 }, (_, i) => fakeFile(`HR/f${i}.txt`, 10));
    const plan = buildFolderPlan(files, { maxSizeBytes: MAX });
    expect(plan.tooManyFiles).toBe(true);
  });
});

describe("buildUploadForm", () => {
  it("transmet relative_path et source_folder à l'API", () => {
    const f = fakeFile("HR/policies/leave.docx", 10);
    const form = buildUploadForm(f, { ragSpaceId: "SP", relativePath: "policies/leave.docx", sourceFolder: "HR" });
    expect(form.get("rag_space_id")).toBe("SP");
    expect(form.get("relative_path")).toBe("policies/leave.docx");
    expect(form.get("source_folder")).toBe("HR");
    expect(form.get("file")).toBeInstanceOf(File);
  });
  it("omet les métadonnées absentes (upload unitaire)", () => {
    const f = fakeFile("a.txt", 10);
    const form = buildUploadForm(f, { ragSpaceId: "SP" });
    expect(form.get("relative_path")).toBeNull();
    expect(form.get("source_folder")).toBeNull();
  });
});

describe("IMPORT_FORMATS (catalogue dérivé d'UPLOAD_ACCEPT)", () => {
  it("ne propose QUE des extensions présentes dans UPLOAD_ACCEPT", () => {
    // Verrou anti-divergence : aucune liste codée en dur indépendante.
    const accepted = new Set(
      UPLOAD_ACCEPT.split(",").map((e) => e.trim().replace(/^\./, "").toLowerCase())
    );
    for (const fmt of IMPORT_FORMATS) {
      // Le pseudo-format « sans extension » n'appartient à aucune chaîne `accept` :
      // il représente l'ABSENCE d'extension, pas une extension supplémentaire.
      if (fmt.key === UNKNOWN_FORMAT_KEY) continue;
      for (const ext of fmt.extensions) expect(accepted.has(ext)).toBe(true);
    }
  });

  it("couvre TOUTES les extensions d'UPLOAD_ACCEPT (aucune perdue en route)", () => {
    const covered = new Set(IMPORT_FORMATS.flatMap((f) => f.extensions));
    for (const ext of SUPPORTED_EXTENSIONS) expect(covered.has(ext)).toBe(true);
  });

  it("regroupe les images sous un format unique", () => {
    const images = IMPORT_FORMATS.find((f) => f.key === "images");
    expect(images?.label).toBe("Images");
    expect([...(images?.extensions ?? [])].sort()).toEqual(
      ["jpeg", "jpg", "png", "tif", "tiff", "webp"].sort()
    );
  });

  it("regroupe Markdown (.md/.markdown) et HTML (.html/.htm)", () => {
    expect([...(IMPORT_FORMATS.find((f) => f.key === "markdown")?.extensions ?? [])].sort()).toEqual([
      "markdown",
      "md",
    ]);
    expect([...(IMPORT_FORMATS.find((f) => f.key === "html")?.extensions ?? [])].sort()).toEqual([
      "htm",
      "html",
    ]);
  });

  it("PowerPoint = .pptx SEUL (jamais .ppt ni .pptm)", () => {
    const ppt = IMPORT_FORMATS.find((f) => f.key === "powerpoint");
    expect(ppt?.label).toBe("PowerPoint");
    expect(ppt?.extensions).toEqual(["pptx"]);
    expect(ppt?.extensions).not.toContain("ppt");
    expect(ppt?.extensions).not.toContain("pptm");
  });

  it("Excel = .xlsx seul", () => {
    expect(IMPORT_FORMATS.find((f) => f.key === "excel")?.extensions).toEqual(["xlsx"]);
  });

  it("associe un fichier à son format, insensible à la casse", () => {
    expect(formatOfFilename("rapport.PDF")?.key).toBe("pdf");
    expect(formatOfFilename("deck.PPTX")?.key).toBe("powerpoint");
    expect(formatOfFilename("photo.JPEG")?.key).toBe("images");
    expect(formatOfFilename("archive.tar.gz")).toBeUndefined();
    expect(formatOfFilename("document.pdf.tmp")).toBeUndefined();
    expect(formatOfFilename("fichier")?.key).toBe(UNKNOWN_FORMAT_KEY);
    expect(formatOfFilename("vieux.ppt")).toBeUndefined();
    expect(formatOfFilename("macros.pptm")).toBeUndefined();
  });
});

describe("summarizeFolderFormats", () => {
  const files = [
    fakeFile("D/a.pdf", 10),
    fakeFile("D/b.pdf", 10),
    fakeFile("D/sub/c.PDF", 10),
    fakeFile("D/d.pptx", 10),
    fakeFile("D/e.png", 10),
    fakeFile("D/f.jpg", 10),
    fakeFile("D/g.exe", 10), // non supporté : jamais proposé
  ];

  it("ne liste que les formats RÉELLEMENT présents, avec leur volumétrie", () => {
    const summary = summarizeFolderFormats(files);
    expect(summary.map((s) => s.format.key)).toEqual(["pdf", "powerpoint", "images"]);
    expect(summary.map((s) => s.count)).toEqual([3, 1, 2]); // .PDF majuscule compté
    // Aucun format absent du dossier n'est proposé.
    expect(summary.map((s) => s.format.key)).not.toContain("word");
  });

  it("ordre déterministe (celui d'UPLOAD_ACCEPT), indépendant de l'ordre des fichiers", () => {
    const a = summarizeFolderFormats(files).map((s) => s.format.key);
    const b = summarizeFolderFormats([...files].reverse()).map((s) => s.format.key);
    expect(a).toEqual(b);
  });

  it("ne mute pas le tableau reçu", () => {
    const input = [...files];
    summarizeFolderFormats(input);
    expect(input).toHaveLength(files.length);
    expect(input[0]).toBe(files[0]);
  });

  it("dossier sans aucun format supporté => aucune case à proposer", () => {
    expect(summarizeFolderFormats([fakeFile("D/x.exe", 10)])).toEqual([]);
  });

  it("présélection par défaut = TOUS les formats présents", () => {
    const summary = summarizeFolderFormats(files);
    expect(defaultSelectedFormats(summary)).toEqual(new Set(["pdf", "powerpoint", "images"]));
  });
});

describe("extensionsOfFormats", () => {
  it("déplie une catégorie en toutes ses extensions", () => {
    expect(extensionsOfFormats(new Set(["images"]))).toEqual(
      new Set(["jpg", "jpeg", "png", "tif", "tiff", "webp"])
    );
  });
  it("sélection vide => aucune extension", () => {
    expect(extensionsOfFormats(new Set())).toEqual(new Set());
  });
  it("ignore une clé inconnue", () => {
    expect(extensionsOfFormats(new Set(["inexistant"]))).toEqual(new Set());
  });
});

describe("buildFolderPlan — filtrage par formats sélectionnés", () => {
  const MAX = 25 * 1024 * 1024;
  // Scénario central de la spec.
  const files = [
    fakeFile("D/a.pdf", 100),
    fakeFile("D/b.docx", 100),
    fakeFile("D/c.pptx", 100),
    fakeFile("D/d.xlsx", 100),
    fakeFile("D/sub/e.PDF", 100),
    fakeFile("D/sub/f.exe", 100),
  ];
  const plan = (keys: string[]) =>
    buildFolderPlan(files, { maxSizeBytes: MAX, selectedExtensions: extensionsOfFormats(new Set(keys)) });

  it("PDF + PowerPoint : envoie exactement a.pdf, c.pptx, sub/e.PDF", () => {
    const p = plan(["pdf", "powerpoint"]);
    expect(p.compatible.map((c) => c.relativePath).sort()).toEqual(["a.pdf", "c.pptx", "sub/e.PDF"]);
    // Décoché ≠ non supporté : les deux motifs restent distincts.
    expect(p.skipped.filter((s) => s.reason === "format_not_selected").map((s) => s.name).sort()).toEqual([
      "b.docx",
      "d.xlsx",
    ]);
    expect(p.skipped.filter((s) => s.reason === "unsupported").map((s) => s.name)).toEqual(["f.exe"]);
    expect(p.totalBytes).toBe(300);
  });

  it("tous les formats sélectionnés = comportement d'origine", () => {
    // 5 supportés (dont sub/e.PDF) ; seul f.exe reste écarté, comme non supporté.
    const p = plan(["pdf", "word", "powerpoint", "excel"]);
    expect(p.compatible).toHaveLength(5);
    expect(p.skipped.filter((s) => s.reason === "format_not_selected")).toHaveLength(0);
    expect(p.skipped.map((s) => s.name)).toEqual(["f.exe"]);
  });

  it("PDF uniquement", () => {
    expect(plan(["pdf"]).compatible.map((c) => c.relativePath).sort()).toEqual(["a.pdf", "sub/e.PDF"]);
  });

  it("sélection VIDE => aucun fichier retenu (jamais « tous »)", () => {
    const p = plan([]);
    expect(p.compatible).toHaveLength(0);
    expect(p.totalBytes).toBe(0);
    expect(p.skipped.filter((s) => s.reason === "format_not_selected")).toHaveLength(5);
  });

  it("catégorie Images : toutes ses extensions, en majuscules comprises", () => {
    const imgs = [
      fakeFile("D/a.png", 10),
      fakeFile("D/b.JPEG", 10),
      fakeFile("D/c.webp", 10),
      fakeFile("D/d.tiff", 10),
      fakeFile("D/e.pdf", 10),
    ];
    const p = buildFolderPlan(imgs, {
      maxSizeBytes: MAX,
      selectedExtensions: extensionsOfFormats(new Set(["images"])),
    });
    expect(p.compatible.map((c) => c.relativePath).sort()).toEqual([
      "a.png",
      "b.JPEG",
      "c.webp",
      "d.tiff",
    ].sort());
    expect(p.skipped.map((s) => s.name)).toEqual(["e.pdf"]);
  });

  it("un format décoché prime sur un motif technique (taille)", () => {
    const p = buildFolderPlan([fakeFile("D/gros.docx", MAX + 1)], {
      maxSizeBytes: MAX,
      selectedExtensions: extensionsOfFormats(new Set(["pdf"])),
    });
    expect(p.skipped[0].reason).toBe("format_not_selected");
  });

  it("un format sélectionné mais trop gros reste écarté pour sa taille", () => {
    const p = buildFolderPlan([fakeFile("D/gros.pdf", MAX + 1)], {
      maxSizeBytes: MAX,
      selectedExtensions: extensionsOfFormats(new Set(["pdf"])),
    });
    expect(p.skipped[0].reason).toBe("too_large");
  });

  it("omettre selectedExtensions conserve le comportement historique (drag & drop)", () => {
    const p = buildFolderPlan(files, { maxSizeBytes: MAX });
    expect(p.compatible).toHaveLength(5); // tous les supportés, aucun choix de format
    expect(p.skipped.filter((s) => s.reason === "format_not_selected")).toHaveLength(0);
  });

  it("préserve l'arborescence : même nom dans deux sous-dossiers", () => {
    const dup = [fakeFile("D/x/rapport.pdf", 10), fakeFile("D/y/rapport.pdf", 10)];
    const p = buildFolderPlan(dup, {
      maxSizeBytes: MAX,
      selectedExtensions: extensionsOfFormats(new Set(["pdf"])),
    });
    expect(p.compatible.map((c) => c.relativePath).sort()).toEqual(["x/rapport.pdf", "y/rapport.pdf"]);
    expect(p.compatible.every((c) => c.sourceFolder === "D")).toBe(true);
  });

  it("ne mute ni les fichiers ni les options reçues", () => {
    const opts = { maxSizeBytes: MAX, selectedExtensions: new Set(["pdf"]) };
    const before = [...files];
    buildFolderPlan(files, opts);
    expect(files).toEqual(before);
    expect(opts.selectedExtensions).toEqual(new Set(["pdf"]));
    expect(opts.maxSizeBytes).toBe(MAX);
  });
});

describe("humanBytes", () => {
  it("formate les tailles", () => {
    expect(humanBytes(0)).toBe("0 o");
    expect(humanBytes(500)).toBe("500 o");
    expect(humanBytes(1024)).toBe("1 Ko");
    expect(humanBytes(187 * 1024 * 1024)).toBe("187 Mo");
  });
});

// ---------------------------------------------------------------------------
// Contrat DYNAMIQUE des formats (lot STAGING-RELEASE) — l'import de dossier ne
// décide plus depuis la constante de module, mais depuis l'`accept` effectif,
// capacités serveur comprises.
//
// Ces tests ne disent RIEN de ce que le serveur annonce réellement en staging :
// ils verrouillent le CÂBLAGE. Voir uploadIfc.test.ts pour le chemin unitaire.
// ---------------------------------------------------------------------------
describe("catalogue de formats piloté par les capacités serveur", () => {
  const MAXB = 50 * 1024 * 1024;

  it("le socle statique ignore les formats sous flag serveur", () => {
    const base = folderFormatCatalog(UPLOAD_ACCEPT);
    for (const ext of ["ifc", "dwg", "dxf", "xml", "msg"]) {
      expect(base.extensions.has(ext)).toBe(false);
    }
  });

  it("un format NON annoncé reste non importable", () => {
    const cat = folderFormatCatalog(uploadAccept(null, null));
    const files = [fakeFile("d/maquette.ifc", 10)];
    expect(isSupportedFilename("maquette.ifc", cat)).toBe(false);
    expect(formatOfFilename("maquette.ifc", cat)).toBeUndefined();
    expect(summarizeFolderFormats(files, cat)).toHaveLength(0);
    const plan = buildFolderPlan(files, { maxSizeBytes: MAXB, catalog: cat });
    expect(plan.compatible).toHaveLength(0);
    expect(plan.skipped[0].reason).toBe("unsupported");
  });

  it("un format ANNONCÉ est reconnu par le scanner de dossier, sans toucher à aucune liste", () => {
    const cat = folderFormatCatalog(uploadAccept(null, ["ifc"]));
    const files = [fakeFile("d/maquette.ifc", 10)];
    expect(isSupportedFilename("maquette.ifc", cat)).toBe(true);
    expect(formatOfFilename("maquette.ifc", cat)?.extensions).toContain("ifc");
    expect(summarizeFolderFormats(files, cat)).toEqual([
      { format: expect.objectContaining({ extensions: ["ifc"] }), count: 1 },
    ]);
    const plan = buildFolderPlan(files, { maxSizeBytes: MAXB, catalog: cat });
    expect(plan.compatible).toHaveLength(1);
    expect(plan.skipped).toHaveLength(0);
  });

  it("plusieurs formats annoncés sont classés indépendamment", () => {
    const cat = folderFormatCatalog(uploadAccept(null, ["xml", "dxf"]));
    const files = [fakeFile("d/a.xml", 5), fakeFile("d/b.dxf", 5), fakeFile("d/c.ifc", 5)];
    const plan = buildFolderPlan(files, { maxSizeBytes: MAXB, catalog: cat });
    expect(plan.compatible.map((p) => p.file.name).sort()).toEqual(["a.xml", "b.dxf"]);
    // `.ifc` n'était pas annoncé : il reste hors catalogue.
    expect(plan.skipped.map((s) => s.name)).toEqual(["c.ifc"]);
    expect(summarizeFolderFormats(files, cat).map((s) => s.count)).toEqual([1, 1]);
  });

  it("une extension inconnue de tous reste non supportée", () => {
    const cat = folderFormatCatalog(uploadAccept(null, ["ifc", "dwg", "xml", "dxf"]));
    expect(isSupportedFilename("truc.xyz", cat)).toBe(false);
  });

  it("les formats de base suivent AUSSI les capacités serveur", () => {
    // Renversement assumé du contrat : le socle de seize extensions n'est plus
    // statique. Un serveur qui n'annonce qu'IFC ne fait plus proposer `.pdf` —
    // l'ancien comportement laissait la modale promettre ce que le backend refusait.
    const seulementIfc = folderFormatCatalog(uploadAccept(null, ["ifc"]));
    expect(isSupportedFilename("a.ifc", seulementIfc)).toBe(true);
    for (const ext of ["pdf", "docx", "xlsx", "pptx", "csv", "txt", "md", "html", "jpg", "png"]) {
      expect(isSupportedFilename(`a.${ext}`, seulementIfc)).toBe(false);
    }
    // Annonce complète : tout redevient proposé, sans toucher au frontend.
    const complet = folderFormatCatalog(
      uploadAccept(null, ["pdf", "docx", "excel", "pptx", "csv", "txt", "markdown", "html", "image", "ifc"],
                   ["jpeg", "png"])
    );
    for (const ext of ["pdf", "docx", "xlsx", "pptx", "csv", "txt", "md", "html", "jpg", "png", "ifc"]) {
      expect(isSupportedFilename(`a.${ext}`, complet)).toBe(true);
    }
  });

  it("les cases à cocher ne couvrent que les extensions du catalogue effectif", () => {
    const cat = folderFormatCatalog(uploadAccept(null, ["xml"]));
    const keys = new Set(cat.formats.map((f) => f.key));
    expect(extensionsOfFormats(keys, cat).has("xml")).toBe(true);
    expect(extensionsOfFormats(keys, cat).has("ifc")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Frontière EXACTE de la limite par fichier : 50 MiB = 52 428 800 octets.
// Contrat : `size <= 52_428_800` accepté, au-delà `too_large`.
// ---------------------------------------------------------------------------
describe("frontière 50 MiB", () => {
  const LIMIT = 52_428_800;
  const cat = folderFormatCatalog(UPLOAD_ACCEPT);
  const plan1 = (size: number) =>
    buildFolderPlan([fakeFile("d/a.pdf", size)], { maxSizeBytes: LIMIT, catalog: cat });

  it("50 MiB vaut exactement 52 428 800 octets", () => {
    expect(50 * 1024 * 1024).toBe(52_428_800);
  });
  it("0 octet est écarté par sa règle propre (`empty`), pas par la taille", () => {
    expect(plan1(0).skipped[0].reason).toBe("empty");
  });
  it("1 octet est accepté", () => {
    expect(plan1(1).compatible).toHaveLength(1);
  });
  it("50 MiB - 1 est accepté", () => {
    expect(plan1(LIMIT - 1).compatible).toHaveLength(1);
  });
  it("50 MiB EXACT est accepté", () => {
    const p = plan1(LIMIT);
    expect(p.compatible).toHaveLength(1);
    expect(p.skipped).toHaveLength(0);
  });
  it("50 MiB + 1 est refusé `too_large`", () => {
    const p = plan1(LIMIT + 1);
    expect(p.compatible).toHaveLength(0);
    expect(p.skipped[0].reason).toBe("too_large");
  });
});
