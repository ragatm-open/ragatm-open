"use client";

import { useTranslations } from "next-intl";

import { documentKindLabel } from "../_lib/previewKind";

/**
 * État explicite pour un document sans aperçu intégré.
 *
 * C'est le remplaçant de la zone blanche : tout format que l'application ne sait
 * pas rendre (CSV, DOCX, PPTX, image, format inconnu) arrive ici, et **jamais**
 * dans une iframe vide. C'est aussi le repli PERMANENT du visualiseur tabulaire :
 * quand l'aperçu d'un classeur échoue, l'utilisateur retrouve cet écran, avec la
 * localisation de la source et le téléchargement de l'original.
 *
 * Invariant : cet écran affiche toujours au moins le nom du fichier, une raison,
 * et un moyen d'agir. Il n'existe aucun chemin par lequel il rende du vide.
 */
export function UnsupportedDocumentViewer(props: {
  filename?: string | null;
  /** Cause lisible. Défaut : format non prévisualisable. */
  reason?: string | null;
  /** Feuille citée, quand la source provient d'un classeur. */
  sheetName?: string | null;
  /** Plages citées, quand elles sont connues. */
  cellRanges?: string[];
  page?: number | null;
  onDownload: () => void;
  onBack: () => void;
}) {
  const t = useTranslations("documentViewer");
  const {
    filename,
    reason,
    sheetName,
    cellRanges = [],
    page,
    onDownload,
    onBack,
  } = props;

  const kind = documentKindLabel(filename) || t("unknownFormat");

  return (
    <div
      style={{
        border: "1px solid #e5e7eb",
        borderRadius: 12,
        background: "white",
        padding: 24,
        display: "flex",
        flexDirection: "column",
        gap: 16,
        alignItems: "flex-start",
      }}
    >
      <div>
        <h2 style={{ margin: "0 0 4px", fontSize: 16 }}>{t("unsupportedTitle")}</h2>
        <p style={{ margin: 0, fontSize: 14, opacity: 0.8 }}>
          {reason || t("unsupportedFormatReason", { kind })}
        </p>
      </div>

      <dl
        style={{
          display: "grid",
          gridTemplateColumns: "auto 1fr",
          gap: "6px 16px",
          margin: 0,
          fontSize: 14,
        }}
      >
        <dt style={{ opacity: 0.7 }}>{t("documentTitle")}</dt>
        <dd style={{ margin: 0, wordBreak: "break-word" }}>{filename || "—"}</dd>

        <dt style={{ opacity: 0.7 }}>{t("fieldFormat")}</dt>
        <dd style={{ margin: 0 }}>{kind}</dd>

        {sheetName ? (
          <>
            <dt style={{ opacity: 0.7 }}>{t("sheet")}</dt>
            <dd style={{ margin: 0, wordBreak: "break-word" }}>{sheetName}</dd>
          </>
        ) : null}

        {cellRanges.length > 0 ? (
          <>
            <dt style={{ opacity: 0.7 }}>{t("fieldCells")}</dt>
            <dd style={{ margin: 0 }}>{cellRanges.join(", ")}</dd>
          </>
        ) : null}

        {page ? (
          <>
            <dt style={{ opacity: 0.7 }}>{t("page")}</dt>
            <dd style={{ margin: 0 }}>{page}</dd>
          </>
        ) : null}
      </dl>

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <button
          onClick={onDownload}
          style={{
            padding: "8px 12px",
            borderRadius: 10,
            border: "1px solid #111827",
            background: "#111827",
            color: "white",
            cursor: "pointer",
          }}
        >
          ⬇️ {t("downloadOriginalShort")}
        </button>
        <button
          onClick={onBack}
          style={{
            padding: "8px 12px",
            borderRadius: 10,
            border: "1px solid #e5e7eb",
            background: "white",
            cursor: "pointer",
          }}
        >
          ← {t("back")}
        </button>
      </div>
    </div>
  );
}
