"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { apiFetchRaw } from "@/lib/api";
import { SpreadsheetGrid } from "./SpreadsheetGrid";
import { SpreadsheetSheetTabs } from "./SpreadsheetSheetTabs";
import { UnsupportedDocumentViewer } from "./UnsupportedDocumentViewer";
import {
  previewErrorMessageKey,
  type TabularPreview,
} from "../_lib/tabularPreview";

type Status = "loading" | "ready" | "error";

/**
 * Visualiseur de classeur — aperçu tabulaire borné.
 *
 * L'ouverture se fait sur la feuille CITÉE, ancrée sur la plage citée, sans que
 * le frontend ait à connaître la structure du classeur : c'est le `chunk_id` qui
 * porte la localisation, et le backend qui la résout. Le texte du snippet n'est
 * jamais analysé.
 *
 * Changer d'onglet relance une requête bornée **sans** `chunk_id` : on consulte
 * alors une autre feuille, et il n'y a plus de citation à surligner. Revenir sur
 * la feuille source restaure le surlignage, parce que le `chunk_id` est réinjecté.
 *
 * En cas d'échec — quel qu'il soit — on retombe sur l'état explicite du Lot 0,
 * jamais sur une zone blanche.
 */
export function SpreadsheetDocumentViewer(props: {
  documentId: string;
  filename: string;
  chunkId: string | null;
  onUnauthorized: () => void;
  onDownload: () => void;
  onBack: () => void;
  onLocationChange?: (loc: { sheetName: string | null; ranges: string[] }) => void;
}) {
  const t = useTranslations("documentViewer");
  const {
    documentId,
    filename,
    chunkId,
    onUnauthorized,
    onDownload,
    onBack,
    onLocationChange,
  } = props;

  const [status, setStatus] = useState<Status>("loading");
  const [preview, setPreview] = useState<TabularPreview | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  // `null` = ouvrir sur la feuille citée (décidée par le backend via chunk_id).
  const [sheetIndex, setSheetIndex] = useState<number | null>(null);

  // Rappels gardés en ref : dérivés de `useRouter()`, leur identité n'est pas
  // garantie stable et les inscrire en dépendances relancerait la requête sans fin.
  const cbRef = useRef({ onUnauthorized, onLocationChange });
  useEffect(() => {
    cbRef.current = { onUnauthorized, onLocationChange };
  }, [onUnauthorized, onLocationChange]);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      setStatus("loading");
      setErrorCode(null);

      const qs = new URLSearchParams();
      // Le chunk n'est transmis que pour la feuille qui le porte : sur une autre
      // feuille, il produirait un conflit de référence — et c'est voulu, le
      // backend refuse d'arbitrer plutôt que d'afficher la mauvaise feuille.
      if (chunkId && sheetIndex === null) qs.set("chunk_id", chunkId);
      if (sheetIndex !== null) qs.set("sheet_index", String(sheetIndex));

      try {
        // `apiFetchRaw` et non `apiGet` : ce dernier réduit le corps d'erreur à
        // son `detail` et PERD le `reason_code`. Or c'est le code qui porte la
        // raison exacte du refus — et donc le message que l'utilisateur doit lire
        // (« document réindexé » n'est pas « feuille inconnue »).
        const res = await apiFetchRaw(
          `/api/v1/documents/${encodeURIComponent(documentId)}/tabular-preview/?${qs}`,
          { method: "GET", headers: { Accept: "application/json" } }
        );

        if (res.status === 401) {
          cbRef.current.onUnauthorized();
          return;
        }

        const body = await res.json().catch(() => null);
        if (cancelled) return;

        if (!res.ok || !body) {
          setErrorCode(body?.reason_code || "network_error");
          setStatus("error");
          cbRef.current.onLocationChange?.({ sheetName: null, ranges: [] });
          return;
        }

        const data = body as TabularPreview;
        setPreview(data);
        setStatus("ready");
        cbRef.current.onLocationChange?.({
          sheetName: data.viewport.sheet_name,
          ranges: data.viewport.requested_ranges,
        });
      } catch {
        if (cancelled) return;
        setErrorCode("network_error");
        setStatus("error");
        cbRef.current.onLocationChange?.({ sheetName: null, ranges: [] });
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [documentId, chunkId, sheetIndex]);

  const selectSheet = useCallback(
    (index: number) => {
      // Revenir sur la feuille citée réactive le surlignage : on repasse en mode
      // « piloté par le chunk » plutôt que de figer un index.
      const citedIndex = preview?.workbook.sheets.find((s) => s.has_citation)?.index;
      setSheetIndex(chunkId && index === citedIndex ? null : index);
    },
    [preview, chunkId]
  );

  if (status === "error") {
    return (
      <UnsupportedDocumentViewer
        filename={filename}
        reason={t(previewErrorMessageKey(errorCode))}
        onDownload={onDownload}
        onBack={onBack}
      />
    );
  }

  if (status === "loading" && !preview) {
    return (
      <div
        style={{
          padding: 24,
          border: "1px solid #e5e7eb",
          borderRadius: 12,
          background: "white",
          opacity: 0.8,
        }}
        aria-live="polite"
      >
        {t("loadingSpreadsheet")}
      </div>
    );
  }

  if (!preview) return null;

  const { viewport, workbook, truncation, fidelity } = preview;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <SpreadsheetSheetTabs
        sheets={workbook.sheets}
        activeIndex={viewport.sheet_index}
        onSelect={selectSheet}
        disabled={status === "loading"}
      />

      <div
        style={{
          display: "flex",
          gap: 12,
          flexWrap: "wrap",
          alignItems: "center",
          fontSize: 12,
          opacity: 0.85,
        }}
      >
        <span>
          {t("sheet")} <strong>{viewport.sheet_name}</strong>
          {viewport.sheet_state === "hidden" ? ` ${t("hiddenInExcel")}` : ""}
        </span>
        {viewport.requested_ranges.length > 0 ? (
          <span>
            {t("citedPassage")} :{" "}
            <strong style={{ background: "#fef3c7", padding: "1px 6px", borderRadius: 4 }}>
              {viewport.requested_ranges.join(", ")}
            </strong>
          </span>
        ) : (
          <span style={{ opacity: 0.7 }}>{t("noCitationOnSheet")}</span>
        )}
        <span style={{ opacity: 0.7 }}>
          {t("contextShown", { range: viewport.display_range })}
        </span>
      </div>

      <SpreadsheetGrid preview={preview} />

      {truncation.truncated ? (
        <div
          role="status"
          style={{
            fontSize: 12,
            padding: "8px 12px",
            borderRadius: 8,
            background: "#fffbeb",
            border: "1px solid #fde68a",
            color: "#92400e",
          }}
        >
          {t("truncatedSpreadsheet", { reason: truncation.reason ?? "" })}
        </div>
      ) : null}

      <SpreadsheetFidelityNotice warnings={fidelity.warnings} />
    </div>
  );
}

/**
 * Bandeau de fidélité — **toujours affiché**.
 *
 * Un aperçu simplifié qui ne dit pas qu'il l'est laisse croire à une fidélité
 * visuelle qu'il n'a pas. L'utilisateur doit savoir que ce qu'il voit sont les
 * valeurs et les coordonnées, pas la mise en forme du classeur.
 */
function SpreadsheetFidelityNotice(props: { warnings: string[] }) {
  const t = useTranslations("documentViewer");
  const { warnings } = props;

  const extra: string[] = [];
  if (warnings.includes("workbook_requires_recalculation")) {
    extra.push(t("fidelityXlsxRecalc"));
  }
  if (warnings.includes("merged_cells_not_rendered")) {
    extra.push(t("fidelityXlsxMerged"));
  }
  if (warnings.includes("external_links_not_followed")) {
    extra.push(t("fidelityXlsxExternalLinks"));
  }

  return (
    <div
      style={{
        fontSize: 12,
        padding: "8px 12px",
        borderRadius: 8,
        background: "#f9fafb",
        border: "1px solid #e5e7eb",
        color: "#374151",
      }}
    >
      <strong>{t("simplifiedPreview")}</strong> — {t("fidelityXlsxBody")}
      {extra.length > 0 ? (
        <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
          {extra.map((t) => (
            <li key={t}>{t}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
