import { describe, it, expect } from "vitest";
import {
  humanError,
  extractionNotice,
  isReadyWithWarnings,
  deleteErrorMessage,
  FILE_COLORS,
  UPLOAD_ACCEPT,
  UPLOAD_FORMATS_LABEL,
} from "@/app/documents/_lib/types";
import { fileTypeLabel } from "@/lib/format";

// Le backend masque le provider dans error_message (anti-fuite) : c'est l'UI qui
// fournit le provider d'embeddings GLOBAL (via /api/v1/capabilities/).
const TRANSIENT = "transient_ingest_error: EmbeddingsError";

describe("humanError — message embeddings adapté au provider", () => {
  it("mentionne Ollama quand le provider d'embeddings est Ollama", () => {
    const msg = humanError(TRANSIENT, "ollama");
    expect(msg).toMatch(/Ollama/);
    expect(msg).toMatch(/OLLAMA_BASE_URL/);
  });

  it("ne mentionne PAS Ollama quand le provider d'embeddings est le pipeline Gateway", () => {
    const msg = humanError(TRANSIENT, "ai_gateway");
    expect(msg).toMatch(/AI Gateway/);
    expect(msg).not.toMatch(/Ollama/i);
  });

  it("reste neutre (sans nom de provider) quand le provider est inconnu", () => {
    const msg = humanError(TRANSIENT);
    expect(msg).not.toMatch(/Ollama/i);
    expect(msg).not.toMatch(/Gateway/i);
    expect(msg).toMatch(/embeddings/i);
  });

  it("déduit le pipeline Gateway du message backend brut si aucun hint fourni", () => {
    const msg = humanError("ai_gateway embeddings request failed: timeout");
    expect(msg).toMatch(/AI Gateway/);
    expect(msg).not.toMatch(/Ollama/i);
  });

  it("le hint UI prime sur le contenu du message", () => {
    // message générique + hint Gateway -> message Gateway
    const msg = humanError("embedding_provider_unavailable", "ai_gateway");
    expect(msg).toMatch(/AI Gateway/);
    expect(msg).not.toMatch(/Ollama/i);
  });

  it("conserve les autres mappings d'erreur (non régressé)", () => {
    expect(humanError("unsupported_file_type")).toMatch(/non supporté/i);
    expect(humanError("extraction_timeout")).toMatch(/Délai/i);
  });

  it("messages OCR distincts selon la cause", () => {
    // PDF sans texte natif : invite à lancer l'OCR à la demande.
    expect(humanError("pdf_scanned_requires_ocr: PDF scanné (image)")).toMatch(
      /Réindexer avec OCR/i,
    );
    // OCR lancé mais aucun texte : message distinct.
    const noText = humanError("ocr_no_text: L'OCR n'a produit aucun texte");
    expect(noText).toMatch(/aucun texte exploitable/i);
    expect(noText).not.toMatch(/Réindexer avec OCR/i);
    // Moteur OCR absent du serveur : message distinct (≠ document illisible).
    const missing = humanError("ocr_engine_missing: Moteur OCR indisponible");
    expect(missing).toMatch(/Moteur OCR non installé/i);
    const raster = humanError("ocr_pdf_rasterizer_missing: ...");
    expect(raster).toMatch(/Moteur OCR non installé/i);
  });
});

describe("extractionNotice / isReadyWithWarnings (P1)", () => {
  it("signale les pages graphiques non indexées sur un doc READY", () => {
    const notice = extractionNotice({
      status: "READY",
      extraction: { status: "ready_with_warnings", image_only_pages: [1] },
    });
    expect(notice).toMatch(/pages graphiques/i);
    expect(notice).toMatch(/page 1/);
  });

  it("signale le moteur PDF alternatif utilisé", () => {
    const notice = extractionNotice({
      status: "READY",
      extraction: { status: "success", native_fallback_used: true, native_fallback_engine: "pymupdf" },
    });
    expect(notice).toMatch(/moteur PDF alternatif/i);
    expect(notice).toMatch(/pymupdf/);
  });

  it("signale requires_visual_check (image / OCR visuel)", () => {
    const notice = extractionNotice({
      status: "READY",
      extraction: { status: "success", type: "image", requires_visual_check: true },
    });
    expect(notice).toMatch(/vérifiez le fichier source/i);
  });

  it("ne signale rien pour un document propre", () => {
    expect(
      extractionNotice({ status: "READY", extraction: { status: "success", empty_pages: [] } }),
    ).toBe("");
    expect(extractionNotice({ status: "READY" })).toBe("");
  });

  it("isReadyWithWarnings vrai si pages vides", () => {
    expect(
      isReadyWithWarnings({ status: "READY", extraction: { status: "success", empty_pages: [1] } }),
    ).toBe(true);
    expect(
      isReadyWithWarnings({ status: "READY", extraction: { status: "success", empty_pages: [] } }),
    ).toBe(false);
  });
});

describe("Images — humanError, formats, badges (Lot 3)", () => {
  it("messages d'erreur image dédiés", () => {
    expect(humanError("image_corrupted")).toMatch(/illisible ou corrompue/i);
    expect(humanError("image_too_large: 50x50")).toMatch(/trop volumineuse ou trop grande/i);
    expect(humanError("image_ingest_disabled")).toMatch(/ingestion des images est désactivée/i);
    expect(humanError("image_unsupported_format")).toMatch(/Format image non supporté/i);
  });

  it("ne régresse pas les messages OCR PDF existants", () => {
    expect(humanError("pdf_scanned_requires_ocr")).toMatch(/Réindexer avec OCR/i);
    expect(humanError("ocr_no_text")).toMatch(/aucun texte exploitable/i);
    expect(humanError("ocr_engine_missing")).toMatch(/Moteur OCR non installé/i);
  });

  it("l'input upload accepte les extensions image MVP", () => {
    for (const ext of [".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"]) {
      expect(UPLOAD_ACCEPT).toContain(ext);
    }
    // sans casser les formats existants
    expect(UPLOAD_ACCEPT).toContain(".pdf");
    expect(UPLOAD_ACCEPT).toContain(".docx");
  });

  it("le libellé d'aide mentionne les formats image", () => {
    expect(UPLOAD_FORMATS_LABEL).toMatch(/JPG/);
    expect(UPLOAD_FORMATS_LABEL).toMatch(/PNG/);
    expect(UPLOAD_FORMATS_LABEL).toMatch(/TIFF/);
    expect(UPLOAD_FORMATS_LABEL).toMatch(/WEBP/);
  });

  it("badge/type image correct (fileTypeLabel + couleur)", () => {
    expect(fileTypeLabel("plan.png")).toBe("PNG");
    expect(fileTypeLabel("scan.JPG")).toBe("JPG");
    expect(fileTypeLabel("doc.tiff")).toBe("TIFF");
    for (const k of ["JPG", "JPEG", "PNG", "TIF", "TIFF", "WEBP"]) {
      expect(FILE_COLORS[k]).toBeTruthy();
    }
    // badges existants intacts
    expect(FILE_COLORS["PDF"]).toBeTruthy();
    expect(FILE_COLORS["DOCX"]).toBeTruthy();
  });
});

describe("Refus Office & sécurité XML (lot E0)", () => {
  it("classeur à macros (.xlsm) -> message dédié, distinct du .docm", () => {
    const msg = humanError("macro_enabled_workbook_refused");
    expect(msg).toMatch(/macros/i);
    expect(msg).toMatch(/\.xlsx/);
    expect(msg).not.toMatch(/Word/i);
  });

  it("document Word à macros (.docm) -> message inchangé (non-régression)", () => {
    expect(humanError("macro_enabled_document_refused")).toMatch(/Word.*macros/i);
  });

  it("XML piégé (DOCTYPE/ENTITY) -> refus explicite, sans jargon", () => {
    const msg = humanError("unsafe_xml: word/document.xml");
    expect(msg).toMatch(/structure XML non autorisée/i);
    expect(msg).not.toMatch(/DOCTYPE|ENTITY/);
  });

  it("Office binaire ancien (.xls) -> conversion recommandée", () => {
    const msg = humanError("legacy_office_format");
    expect(msg).toMatch(/\.xls/i);
    expect(msg).toMatch(/CSV|\.docx/);
  });

  it("classeur OpenDocument (.ods) -> conversion recommandée, pas « archive »", () => {
    const msg = humanError("unsupported_spreadsheet_format");
    expect(msg).toMatch(/OpenDocument|\.ods/i);
    expect(msg).toMatch(/CSV/);
    expect(msg).not.toMatch(/archive/i);
  });

  it("`.xlsx` est ouvert (E4) — les autres tableurs restent fermés", () => {
    expect(humanError("unsupported_office_format")).toMatch(/non support/i);
    // Depuis le lot E4, l'upload propose `.xlsx` — et LUI SEUL.
    expect(UPLOAD_ACCEPT).toContain(".xlsx");
    expect(UPLOAD_ACCEPT).not.toMatch(/\.xlsm|\.ods|\.xlsb/);
    expect(UPLOAD_ACCEPT.split(",")).not.toContain(".xls");
    expect(UPLOAD_FORMATS_LABEL).toMatch(/Excel \(\.xlsx\)/);
  });
});

describe("deleteErrorMessage", () => {
  it("409 / ingestion en cours -> message dédié", () => {
    expect(deleteErrorMessage("document_processing")).toMatch(/en cours de traitement/i);
    expect(deleteErrorMessage("HTTP 409")).toMatch(/en cours de traitement/i);
  });
  it("403/404 -> impossible ou introuvable", () => {
    expect(deleteErrorMessage("not_found")).toMatch(/impossible ou.*introuvable/i);
    expect(deleteErrorMessage("HTTP 403")).toMatch(/impossible ou.*introuvable/i);
  });
  it("autre -> message générique", () => {
    expect(deleteErrorMessage("")).toMatch(/Échec de la suppression/i);
  });
});
