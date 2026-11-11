import { describe, expect, it } from "vitest";

import {
  srcEmailAttachment,
  srcEmailSubject,
  srcPage,
  srcSheet,
  srcViewerHref,
} from "@/app/chat/_lib/sources";
import { emailAttachmentSummary, extractionNotice, humanError, uploadAccept } from "@/app/documents/_lib/types";

/**
 * citation HIÉRARCHIQUE d'un courriel : parent → pièce jointe → repère.
 *
 * Invariants : une pièce n'est citée que depuis `meta.email_part === "attachment"`, jamais
 * déduite du nom de fichier ; la page d'une pièce reste affichée mais n'ancre JAMAIS le
 * lien vers le courriel parent (qui n'a pas de pages).
 */

const PDF_ATTACHMENT = {
  rank: 1,
  document_id: "d-mail",
  chunk_id: "c-7",
  filename: "reunion.msg",
  page_number: 2,
  snippet: "Pièce jointe : rapport.pdf — architecture",
  meta: {
    email_part: "attachment",
    email_subject: "Réunion de validation",
    email_attachment_filename: "rapport.pdf",
    email_attachment_path: "1",
  },
};

const BODY = {
  rank: 2,
  document_id: "d-mail",
  chunk_id: "c-1",
  filename: "reunion.msg",
  page_number: null,
  snippet: "Merci de valider le budget",
  meta: { email_part: "body", email_subject: "Réunion de validation" },
};

describe("citation d'un courriel et de ses pièces jointes", () => {
  it("cite le courriel parent ET la pièce jointe", () => {
    expect(srcEmailSubject(PDF_ATTACHMENT)).toBe("Réunion de validation");
    expect(srcEmailAttachment(PDF_ATTACHMENT)).toEqual({ filename: "rapport.pdf", path: "1" });
    expect(srcPage(PDF_ATTACHMENT)).toBe(2); // repère interne de la pièce, conservé
  });

  it("le corps n'est jamais présenté comme une pièce jointe", () => {
    expect(srcEmailSubject(BODY)).toBe("Réunion de validation");
    expect(srcEmailAttachment(BODY)).toBeNull();
  });

  it("aucun ancrage de page vers le courriel parent pour une pièce", () => {
    const href = srcViewerHref(PDF_ATTACHMENT) ?? "";
    expect(href).toContain("/doc/d-mail");
    expect(href).not.toContain("page=");
  });

  it("un document ordinaire n'est pas affecté", () => {
    const xlsx = { ...BODY, meta: { kind: "excel_table", sheet_name: "Prévisions" } };
    expect(srcEmailSubject(xlsx)).toBeNull();
    expect(srcEmailAttachment(xlsx)).toBeNull();
    expect(srcSheet(xlsx)).toBe("Prévisions");
  });

  it("un nom seul ne fait pas une pièce citée", () => {
    const forged = { ...BODY, meta: { email_attachment_filename: "faux.pdf" } };
    expect(srcEmailAttachment(forged)).toBeNull();
  });
});

describe("résumé des pièces jointes d'un courriel", () => {
  const ext = {
    attachment_ingestion: "enabled" as const,
    attachments_detected: 4,
    attachments: [
      { filename: "a.pdf", extension: "pdf", mimetype: "", size_bytes: 1, content_id: "", is_inline: false, status: "INDEXED" as const },
      { filename: "b.zip", extension: "zip", mimetype: "", size_bytes: 1, content_id: "", is_inline: false, status: "REJECTED" as const },
      { filename: "c.pdf", extension: "pdf", mimetype: "", size_bytes: 1, content_id: "", is_inline: false, status: "FAILED" as const },
      { filename: "d.txt", extension: "txt", mimetype: "", size_bytes: 1, content_id: "", is_inline: false, status: "SKIPPED_LIMIT" as const },
    ],
  };

  it("dit ce qui est interrogeable et ce qui ne l'est pas", () => {
    const text = emailAttachmentSummary(ext);
    expect(text).toContain("1 pièce jointe interrogeable sur 4");
    expect(text).toContain("refusée");
    expect(text).toContain("en échec");
    expect(text).toContain("limite");
    expect(extractionNotice({ status: "READY", extraction: ext })).toContain("interrogeable");
  });

  it("une image sans texte n'est ni une erreur ni un contenu indexé (27C)", () => {
    const rows = [
      { filename: "logo.png", extension: "png", mimetype: "", size_bytes: 1, content_id: "c", is_inline: true, status: "NO_EXTRACTABLE_TEXT" as const },
      { filename: "anim.gif", extension: "gif", mimetype: "", size_bytes: 1, content_id: "", is_inline: true, status: "UNSUPPORTED" as const },
      { filename: "graph.png", extension: "png", mimetype: "", size_bytes: 1, content_id: "", is_inline: true, status: "INDEXED" as const },
    ];
    const text = emailAttachmentSummary({ attachment_ingestion: "enabled", attachments: rows });
    expect(text).toContain("1 pièce jointe interrogeable sur 3");
    expect(text).toContain("1 sans texte exploitable");
    expect(text).toContain("1 non prise en charge");
    expect(text).not.toContain("échec");
  });

  it("ingestion fermée : aucun résumé trompeur", () => {
    expect(emailAttachmentSummary({ ...ext, attachment_ingestion: "disabled" })).toBe("");
  });

  it(".eml proposé seulement si le serveur l'annonce", () => {
    expect(uploadAccept(null, ["pdf"], [])).not.toContain(".eml");
    expect(uploadAccept(null, ["pdf", "eml"], [])).toContain(".eml");
  });

  it("refus d'espace et EML désactivé : messages nommés", () => {
    expect(humanError("email_ingestion_not_enabled_for_space")).toContain("pas activés pour cet espace");
    expect(humanError("eml_ingestion_disabled")).toContain(".eml");
  });
});
