"use client";

import { useTranslations } from "next-intl";

/**
 * Barre d'outils du visualiseur de document.
 *
 * Les libellés étaient implicitement PDF : « Télécharger le PDF » et une ligne
 * « Page — » affichée même pour un classeur, qui n'a pas de pages (`page_number`
 * est `None` à l'ingestion pour Excel : une feuille n'est pas une page). Un
 * classeur affiche donc désormais sa FEUILLE et sa PLAGE, un PDF sa page, et le
 * téléchargement parle de « l'original » quel que soit le format.
 */
export function DocumentToolbar(props: {
  title: string;
  /** PDF uniquement. `null` pour tout format sans pagination. */
  page?: number | null;
  /** Classeur : feuille citée. */
  sheetName?: string | null;
  /** Classeur : plages citées. Plusieurs si le chunk est découpé par colonnes. */
  cellRanges?: string[];
  onBack: () => void;
  onDownload: () => void;
  /** Ouverture dans un onglet : uniquement quand une URL rendable existe (PDF). */
  openHref?: string | null;
}) {
  const t = useTranslations("documentViewer");
  const {
    title,
    page = null,
    sheetName = null,
    cellRanges = [],
    onBack,
    onDownload,
    openHref = null,
  } = props;

  // Une seule ligne de contexte, choisie par ce que le document EST — jamais un
  // « Page — » de remplissage, qui laissait croire à une pagination absente.
  const subtitleParts: string[] = [];
  if (sheetName) subtitleParts.push(t("sheetLabel", { name: sheetName }));
  if (cellRanges.length > 0)
    subtitleParts.push(t("cellsLabel", { ranges: cellRanges.join(", ") }));
  if (!sheetName && page) subtitleParts.push(t("pageNumber", { page }));
  const subtitle = subtitleParts.join(" · ");

  return (
    <div
      style={{
        display: "flex",
        gap: 10,
        alignItems: "center",
        justifyContent: "space-between",
        padding: 12,
        border: "1px solid #e5e7eb",
        borderRadius: 12,
        background: "white",
      }}
    >
      <div style={{ minWidth: 0 }}>
        <div
          style={{
            fontWeight: 700,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
          title={title}
        >
          {title}
        </div>
        {subtitle ? (
          <div style={{ fontSize: 12, opacity: 0.75 }}>{subtitle}</div>
        ) : null}
      </div>

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", justifyContent: "flex-end" }}>
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

        <button
          onClick={onDownload}
          style={{
            padding: "8px 12px",
            borderRadius: 10,
            border: "1px solid #e5e7eb",
            background: "white",
            cursor: "pointer",
          }}
          title={t("downloadOriginalShort")}
        >
          ⬇️ {t("downloadOriginalShort")}
        </button>

        {openHref ? (
          <a
            href={openHref}
            target="_blank"
            rel="noreferrer"
            style={{
              padding: "8px 12px",
              borderRadius: 10,
              border: "1px solid #111827",
              background: "#111827",
              color: "white",
              textDecoration: "none",
              cursor: "pointer",
            }}
            title={t("openInNewTab")}
          >
            {t("open")} ↗
          </a>
        ) : null}
      </div>
    </div>
  );
}
