import { describe, expect, it } from "vitest";

import {
  buildFolderPlan,
  defaultSelectedFormats,
  extensionsOfFormats,
  isOfficeTemporaryFilename,
  summarizeFolderFormats,
} from "@/app/documents/_lib/folderImport";
import { humanError } from "@/app/documents/_lib/types";

/**
 * Les verrous temporaires Office (`~$rapport.docx`) ne doivent jamais partir au serveur.
 *
 * Word/Excel les écrivent à l'ouverture d'un document et les suppriment à la fermeture ;
 * ceux qui restent viennent d'un plantage. Ils portent l'extension du document mais n'en
 * sont pas : ~160 octets contenant le nom du propriétaire de la session. Le backend les
 * refusait déjà — mais un par un, avec un message trompeur et 18 lignes rouges à l'écran.
 */

function fakeFile(relPath: string, size = 1000, name?: string): File {
  const base = name ?? relPath.split("/").pop() ?? relPath;
  const f = new File([new Uint8Array(Math.max(0, size))], base, { type: "" });
  Object.defineProperty(f, "webkitRelativePath", { value: relPath, configurable: true });
  return f;
}

describe("isOfficeTemporaryFilename — la règle canonique", () => {
  it.each([
    "~$rapport.docx",
    "~$budget.xlsx",
    "~$presentation.pptx",
    "~$Document sans titre.docx",
    "~$rapport.pdf",
  ])("détecte %s", (name) => {
    expect(isOfficeTemporaryFilename(name)).toBe(true);
  });

  it("détecte quelle que soit la casse de l'extension", () => {
    expect(isOfficeTemporaryFilename("~$RAPPORT.DOCX")).toBe(true);
  });

  it("gère l'Unicode", () => {
    expect(isOfficeTemporaryFilename("~$présentation.pptx")).toBe(true);
    expect(isOfficeTemporaryFilename("~$рапорт.docx")).toBe(true);
    expect(isOfficeTemporaryFilename("~$日本語.xlsx")).toBe(true);
  });

  it("porte sur le BASENAME, pas sur le chemin (séparateur Unix)", () => {
    expect(isOfficeTemporaryFilename("sub/~$annexe.docx")).toBe(true);
    expect(isOfficeTemporaryFilename("a/b/c/~$deep.xlsx")).toBe(true);
  });

  it("porte sur le BASENAME, pas sur le chemin (séparateur Windows)", () => {
    expect(isOfficeTemporaryFilename("sub\\~$annexe.docx")).toBe(true);
    expect(isOfficeTemporaryFilename("C:\\Users\\d\\~$note.docx")).toBe(true);
  });

  it("un dossier nommé ~$ ne contamine pas ses fichiers", () => {
    // Le marqueur est dans le chemin, pas dans le basename : `rapport.docx` est un vrai
    // document. Détecter sur le chemin complet le condamnerait à tort.
    expect(isOfficeTemporaryFilename("~$dossier/rapport.docx")).toBe(false);
  });

  it.each([
    "rapport~$.docx", // ~$ au MILIEU : le marqueur est un préfixe
    "rapport~$suffixe.docx",
    "~rapport.docx", // tilde seul
    "$rapport.docx", // dollar seul
    "document.docx",
    "$~rapport.docx", // ordre inversé
  ])("n'est PAS un faux positif : %s", (name) => {
    expect(isOfficeTemporaryFilename(name)).toBe(false);
  });

  it.each(["", "~", "$", "fichier_sans_extension", "."])(
    "ne casse pas sur le cas dégénéré %o",
    (name) => {
      expect(typeof isOfficeTemporaryFilename(name)).toBe("boolean");
    }
  );

  it("`~$` nu est un verrou (nom tronqué), pas un crash", () => {
    expect(isOfficeTemporaryFilename("~$")).toBe(true);
  });
});

describe("buildFolderPlan — les verrous sont écartés avant tout envoi", () => {
  const MAX = 25 * 1024 * 1024;

  const MIXED = [
    fakeFile("D/rapport.pdf"),
    fakeFile("D/~$rapport.pdf"),
    fakeFile("D/note.docx"),
    fakeFile("D/~$note.docx"),
    fakeFile("D/presentation.pptx"),
    fakeFile("D/~$presentation.pptx"),
    fakeFile("D/budget.xlsx"),
    fakeFile("D/~$budget.xlsx"),
  ];

  it("4 vrais documents, 4 verrous, 0 « format non supporté »", () => {
    const plan = buildFolderPlan(MIXED, { maxSizeBytes: MAX });
    expect(plan.compatible).toHaveLength(4);
    expect(plan.skipped.filter((s) => s.reason === "office_temporary_file")).toHaveLength(4);
    expect(plan.skipped.filter((s) => s.reason === "unsupported")).toHaveLength(0);
  });

  it("aucun verrou ne figure parmi les fichiers à importer", () => {
    const plan = buildFolderPlan(MIXED, { maxSizeBytes: MAX });
    for (const c of plan.compatible) {
      expect(isOfficeTemporaryFilename(c.file.name)).toBe(false);
    }
  });

  it("le motif est DISTINCT de « non supporté » et de « décoché »", () => {
    const plan = buildFolderPlan([fakeFile("D/~$a.docx"), fakeFile("D/virus.exe")], {
      maxSizeBytes: MAX,
    });
    const byName = Object.fromEntries(plan.skipped.map((s) => [s.name, s.reason]));
    expect(byName["~$a.docx"]).toBe("office_temporary_file");
    expect(byName["virus.exe"]).toBe("unsupported");
  });

  it("un verrou dans un sous-dossier est écarté, chemin relatif conservé", () => {
    const plan = buildFolderPlan([fakeFile("D/sub/~$annexe.xlsx")], { maxSizeBytes: MAX });
    expect(plan.skipped[0].reason).toBe("office_temporary_file");
    expect(plan.skipped[0].relativePath).toBe("sub/~$annexe.xlsx");
  });

  it("le verrou prime sur la taille : jamais classé « vide » ni « trop gros »", () => {
    // Un verrou fait ~160 o, mais un verrou VIDE existe aussi. Le classer « vide »
    // laisserait croire que le fichier est récupérable.
    const plan = buildFolderPlan(
      [fakeFile("D/~$vide.docx", 0), fakeFile("D/~$gros.docx", MAX + 1)],
      { maxSizeBytes: MAX }
    );
    expect(plan.skipped.map((s) => s.reason)).toEqual([
      "office_temporary_file",
      "office_temporary_file",
    ]);
  });

  it("les verrous ne comptent pas dans la taille totale", () => {
    const plan = buildFolderPlan([fakeFile("D/a.pdf", 500), fakeFile("D/~$a.pdf", 9999)], {
      maxSizeBytes: MAX,
    });
    expect(plan.totalBytes).toBe(500);
  });

  it("totalFound compte TOUT ce qui a été détecté, verrous compris", () => {
    const plan = buildFolderPlan(MIXED, { maxSizeBytes: MAX });
    expect(plan.totalFound).toBe(8);
  });

  it("un dossier de verrous UNIQUEMENT ne produit aucun fichier à importer", () => {
    const plan = buildFolderPlan([fakeFile("D/~$a.docx"), fakeFile("D/~$b.xlsx")], {
      maxSizeBytes: MAX,
    });
    expect(plan.compatible).toHaveLength(0);
    expect(plan.skipped).toHaveLength(2);
  });

  it("ne mute pas les entrées", () => {
    const files = [fakeFile("D/~$a.docx")];
    const snapshot = files.map((f) => f.name);
    buildFolderPlan(files, { maxSizeBytes: MAX });
    expect(files.map((f) => f.name)).toEqual(snapshot);
  });

  it("le cas réel : 200 documents + 18 verrous", () => {
    const files = [
      ...Array.from({ length: 200 }, (_, i) => fakeFile(`D/doc${i}.xlsx`)),
      ...Array.from({ length: 18 }, (_, i) => fakeFile(`D/~$doc${i}.xlsx`)),
    ];
    const plan = buildFolderPlan(files, { maxSizeBytes: MAX });
    expect(plan.compatible).toHaveLength(200);
    expect(plan.skipped.filter((s) => s.reason === "office_temporary_file")).toHaveLength(18);
  });
});

describe("summarizeFolderFormats — les verrous ne gonflent pas les compteurs", () => {
  it("« Excel — 1 fichier », pas 2", () => {
    const summaries = summarizeFolderFormats([
      fakeFile("D/budget.xlsx"),
      fakeFile("D/~$budget.xlsx"),
    ]);
    expect(summaries).toHaveLength(1);
    expect(summaries[0].format.key).toBe("excel");
    expect(summaries[0].count).toBe(1);
  });

  it("le scénario de la mission : Word 1, Excel 1", () => {
    const summaries = summarizeFolderFormats([
      fakeFile("D/rapport.docx"),
      fakeFile("D/~$rapport.docx"),
      fakeFile("D/budget.xlsx"),
      fakeFile("D/~$budget.xlsx"),
    ]);
    expect(summaries.map((s) => [s.format.key, s.count])).toEqual([
      ["word", 1],
      ["excel", 1],
    ]);
  });

  it("un format présent UNIQUEMENT sous forme de verrous n'est pas proposé", () => {
    // Proposer « PowerPoint » alors qu'aucun .pptx n'est importable serait un mensonge :
    // cocher la case n'importerait rien.
    const summaries = summarizeFolderFormats([
      fakeFile("D/rapport.docx"),
      fakeFile("D/~$deck.pptx"),
    ]);
    expect(summaries.map((s) => s.format.key)).toEqual(["word"]);
  });

  it("la présélection ne retient que les formats réellement importables", () => {
    const summaries = summarizeFolderFormats([
      fakeFile("D/rapport.docx"),
      fakeFile("D/~$deck.pptx"),
    ]);
    expect([...defaultSelectedFormats(summaries)]).toEqual(["word"]);
  });
});

describe("sélection de formats et verrous — cohérence", () => {
  const MAX = 25 * 1024 * 1024;

  it("décocher un format ne ressuscite pas ses verrous", () => {
    const files = [fakeFile("D/budget.xlsx"), fakeFile("D/~$budget.xlsx")];
    const plan = buildFolderPlan(files, {
      maxSizeBytes: MAX,
      selectedExtensions: extensionsOfFormats(new Set(["excel"])),
    });
    expect(plan.compatible).toHaveLength(1);
    expect(plan.skipped[0].reason).toBe("office_temporary_file");
  });

  it("un verrou d'un format décoché reste « verrou », pas « décoché »", () => {
    // Le motif doit dire la VRAIE raison : il n'aurait pas été importé de toute façon.
    const plan = buildFolderPlan([fakeFile("D/~$budget.xlsx"), fakeFile("D/budget.xlsx")], {
      maxSizeBytes: MAX,
      selectedExtensions: new Set(), // rien de coché
    });
    const byName = Object.fromEntries(plan.skipped.map((s) => [s.name, s.reason]));
    expect(byName["~$budget.xlsx"]).toBe("office_temporary_file");
    expect(byName["budget.xlsx"]).toBe("format_not_selected");
  });
});

describe("message backend", () => {
  it("un verrou posté directement reçoit un message explicite", () => {
    const msg = humanError("office_temporary_file");
    expect(msg).toMatch(/temporaire/i);
    expect(msg).toMatch(/~\$/);
    // Régression visée : « Type de fichier non supporté » — le message d'origine, qui
    // laissait croire à un format légitime non pris en charge.
    expect(msg).not.toMatch(/non supporté/i);
  });
});

describe("non-régression — les vrais documents passent toujours", () => {
  const MAX = 25 * 1024 * 1024;

  it.each(["rapport.docx", "budget.xlsx", "deck.pptx", "note.pdf"])(
    "%s reste importable",
    (name) => {
      const plan = buildFolderPlan([fakeFile(`D/${name}`)], { maxSizeBytes: MAX });
      expect(plan.compatible).toHaveLength(1);
      expect(plan.skipped).toHaveLength(0);
    }
  );

  it("un fichier au tilde légitime n'est pas écarté", () => {
    const plan = buildFolderPlan([fakeFile("D/rapport~$.docx")], { maxSizeBytes: MAX });
    expect(plan.compatible).toHaveLength(1);
  });
});
