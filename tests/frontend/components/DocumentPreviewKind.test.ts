import { describe, it, expect } from "vitest";
import {
  resolvePreviewKind,
  inferPreviewKind,
  documentKindLabel,
} from "@/app/doc/[documentId]/_lib/previewKind";

const XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
const DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
const PPTX =
  "application/vnd.openxmlformats-officedocument.presentationml.presentation";

describe("resolvePreviewKind — la décision backend fait autorité", () => {
  it("respecte preview_kind fourni par le backend", () => {
    expect(resolvePreviewKind({ preview_kind: "spreadsheet" })).toBe("spreadsheet");
    expect(resolvePreviewKind({ preview_kind: "pdf" })).toBe("pdf");
    expect(resolvePreviewKind({ preview_kind: "text" })).toBe("text");
    expect(resolvePreviewKind({ preview_kind: "unsupported" })).toBe("unsupported");
  });

  it("ignore une valeur inconnue et retombe sur le repli", () => {
    // Un backend futur pourrait introduire un genre que ce frontend ignore : on ne
    // le propage pas tel quel (il ne correspondrait à aucun visualiseur), on infère.
    expect(
      resolvePreviewKind({ preview_kind: "hologram", content_type: XLSX })
    ).toBe("spreadsheet");
  });

  it("infère quand le backend est plus ancien et n'envoie pas le champ", () => {
    expect(resolvePreviewKind({ content_type: XLSX, filename: "a.xlsx" })).toBe(
      "spreadsheet"
    );
    expect(resolvePreviewKind({ content_type: "application/pdf" })).toBe("pdf");
  });
});

describe("inferPreviewKind — repli défensif", () => {
  it("le type MIME prime sur l'extension", () => {
    // Doctrine du dépôt : ne jamais faire confiance à l'extension. Un DOCX renommé
    // en .pdf va au visualiseur documentaire, jamais au lecteur PDF ; un PPTX
    // renommé en .pdf n'est pas rendable du tout.
    expect(inferPreviewKind({ contentType: DOCX, filename: "piege.pdf" })).toBe(
      "document"
    );
    expect(inferPreviewKind({ contentType: PPTX, filename: "piege.pdf" })).toBe(
      "presentation"
    );
  });

  it("PPTX est routé vers le visualiseur de présentation", () => {
    expect(inferPreviewKind({ contentType: PPTX })).toBe("presentation");
    expect(inferPreviewKind({ contentType: "", filename: "d.pptx" })).toBe(
      "presentation"
    );
  });

  it("un PPTM (macro) n'est jamais prévisualisé", () => {
    expect(
      inferPreviewKind({
        contentType: "application/vnd.ms-powerpoint.presentation.macroEnabled.12",
        filename: "m.pptm",
      })
    ).toBe("unsupported");
  });

  it("DOCX est routé vers le visualiseur documentaire", () => {
    expect(inferPreviewKind({ contentType: DOCX })).toBe("document");
    expect(inferPreviewKind({ contentType: "", filename: "r.docx" })).toBe("document");
  });

  it("l'extension ne sert que si le type est absent", () => {
    expect(inferPreviewKind({ contentType: "", filename: "a.xlsx" })).toBe("spreadsheet");
    expect(inferPreviewKind({ contentType: "", filename: "a.pdf" })).toBe("pdf");
    expect(inferPreviewKind({ contentType: "", filename: "a.md" })).toBe("text");
  });

  it("octet-stream est traité comme un type absent", () => {
    expect(
      inferPreviewKind({ contentType: "application/octet-stream", filename: "a.xlsx" })
    ).toBe("spreadsheet");
  });

  it("tolère les paramètres de charset", () => {
    expect(
      inferPreviewKind({ contentType: "application/pdf; charset=binary" })
    ).toBe("pdf");
  });

  it("CSV n'est PAS annoncé comme tableur (aucune localisation structurée)", () => {
    // Les chunks CSV passent par le chunker texte générique : ni feuille, ni plage,
    // ni ligne. Le visualiseur tabulaire ne saurait pas où surligner.
    expect(inferPreviewKind({ contentType: "text/csv", filename: "a.csv" })).toBe(
      "unsupported"
    );
    expect(inferPreviewKind({ contentType: "", filename: "a.csv" })).toBe("unsupported");
  });

  it("un format inconnu ne prétend jamais être rendable", () => {
    expect(inferPreviewKind({ contentType: "", filename: "a.bin" })).toBe("unsupported");
    expect(inferPreviewKind({ contentType: "", filename: "" })).toBe("unsupported");
    expect(inferPreviewKind({})).toBe("unsupported");
  });

  it("un classeur macro (.xlsm) n'est jamais prévisualisé", () => {
    expect(
      inferPreviewKind({
        contentType: "application/vnd.ms-excel.sheet.macroEnabled.12",
        filename: "m.xlsm",
      })
    ).toBe("unsupported");
  });
});

describe("documentKindLabel", () => {
  it("rend l'extension en majuscules", () => {
    expect(documentKindLabel("REFERENTIEL.xlsx")).toBe("XLSX");
    expect(documentKindLabel("a.CSV")).toBe("CSV");
  });

  it("renvoie une chaîne vide sans extension (le libellé « Format inconnu » est localisé dans le composant)", () => {
    // Contrat i18n : la fonction pure ne porte plus de libellé français ; le
    // composant substitue `t("unknownFormat")` quand le retour est vide.
    expect(documentKindLabel("sans-extension")).toBe("");
    expect(documentKindLabel(null)).toBe("");
  });
});
