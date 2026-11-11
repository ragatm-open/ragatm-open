"use client";

import { useTranslations } from "next-intl";

import type { TabularPreview } from "../_lib/tabularPreview";
import { cellFlagLabelKeys } from "../_lib/tabularPreview";

/**
 * Grille de cellules — trois niveaux de lecture, visuellement distincts :
 *
 *  1. **passage cité**   fond ambré + bordure ;
 *  2. **zone de contexte** neutre, autour de la citation ;
 *  3. **reste de la feuille** hors fenêtre — non chargé (voir doc backend).
 *
 * Les valeurs sont du contenu NON FIABLE. Elles sont rendues comme nœuds texte
 * React, qui échappe par construction : aucun `dangerouslySetInnerHTML`, aucun
 * lien activé, aucune interprétation Markdown. Un `<script>` dans une cellule
 * s'affiche donc littéralement.
 */
export function SpreadsheetGrid(props: { preview: TabularPreview }) {
  const t = useTranslations("documentViewer");
  const { preview } = props;
  const { columns, rows, viewport } = preview;

  if (rows.length === 0) {
    return (
      <div style={{ padding: 24, opacity: 0.8 }}>
        {t("emptySheetArea")}
      </div>
    );
  }

  const rowLabelWidth = 64;

  return (
    <div
      role="region"
      aria-label={t("gridAriaLabel", {
        sheet: viewport.sheet_name,
        range: viewport.display_range,
      })}
      tabIndex={0}
      style={{
        overflow: "auto",
        maxHeight: "calc(100vh - 320px)",
        border: "1px solid #e5e7eb",
        borderRadius: 12,
        background: "white",
      }}
    >
      <table
        style={{
          borderCollapse: "separate",
          borderSpacing: 0,
          fontSize: 13,
          fontVariantNumeric: "tabular-nums",
          minWidth: "100%",
        }}
      >
        <caption style={{ position: "absolute", width: 1, height: 1, overflow: "hidden", clip: "rect(0 0 0 0)" }}>
          {t("gridCaption", {
            sheet: viewport.sheet_name,
            range: viewport.display_range,
          })}
        </caption>

        <thead>
          <tr>
            <th
              scope="col"
              style={{
                position: "sticky",
                left: 0,
                top: 0,
                zIndex: 3,
                width: rowLabelWidth,
                minWidth: rowLabelWidth,
                background: "#f9fafb",
                borderBottom: "1px solid #e5e7eb",
                borderRight: "1px solid #e5e7eb",
                padding: "6px 8px",
              }}
            >
              <span aria-hidden="true">#</span>
              <span style={{ position: "absolute", width: 1, height: 1, overflow: "hidden", clip: "rect(0 0 0 0)" }}>
                {t("rowNumber")}
              </span>
            </th>
            {columns.map((c) => (
              <th
                key={c.index}
                scope="col"
                data-col={c.letter}
                style={{
                  position: "sticky",
                  top: 0,
                  zIndex: 2,
                  background: "#f9fafb",
                  borderBottom: "1px solid #e5e7eb",
                  borderRight: "1px solid #f3f4f6",
                  padding: "6px 10px",
                  textAlign: "left",
                  whiteSpace: "nowrap",
                  maxWidth: 280,
                }}
                title={c.header || c.letter}
              >
                <div style={{ fontWeight: 700, opacity: 0.6, fontSize: 11 }}>{c.letter}</div>
                {c.header ? (
                  <div
                    style={{
                      fontWeight: 600,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      maxWidth: 260,
                    }}
                  >
                    {c.header}
                  </div>
                ) : null}
              </th>
            ))}
          </tr>
        </thead>

        <tbody>
          {rows.map((row) => (
            <tr key={row.row_number}>
              <th
                scope="row"
                style={{
                  position: "sticky",
                  left: 0,
                  zIndex: 1,
                  background: "#f9fafb",
                  borderRight: "1px solid #e5e7eb",
                  borderBottom: "1px solid #f3f4f6",
                  padding: "4px 8px",
                  textAlign: "right",
                  fontWeight: 500,
                  opacity: 0.7,
                  fontSize: 11,
                }}
              >
                {row.row_number}
              </th>

              {row.cells.map((cell) => {
                const labels = cellFlagLabelKeys(cell.flags).map((k) =>
                  t.has(k) ? t(k) : k
                );
                const isError = cell.flags.includes("error_cell");
                const isUncached = cell.flags.includes("uncached_formula");
                const title = [
                  cell.full_length
                    ? t("truncatedValueChars", { count: cell.full_length })
                    : null,
                  ...labels,
                ]
                  .filter(Boolean)
                  .join(" · ");

                return (
                  <td
                    key={cell.column_index}
                    data-highlighted={cell.highlighted ? "true" : undefined}
                    aria-current={cell.highlighted ? "true" : undefined}
                    title={title || undefined}
                    style={{
                      padding: "4px 10px",
                      borderBottom: "1px solid #f3f4f6",
                      borderRight: "1px solid #f9fafb",
                      background: cell.highlighted ? "#fef3c7" : undefined,
                      boxShadow: cell.highlighted ? "inset 0 0 0 1px #f59e0b" : undefined,
                      color: isError ? "#b91c1c" : undefined,
                      fontStyle: isUncached ? "italic" : undefined,
                      maxWidth: 320,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "pre-wrap",
                      wordBreak: "break-word",
                    }}
                  >
                    {/* Contenu non fiable : nœud texte, jamais de HTML injecté. */}
                    {cell.value}
                    {isUncached && !cell.value ? (
                      <span style={{ opacity: 0.55, fontSize: 11 }}>
                        {t("formulaNotRecalculated")}
                      </span>
                    ) : null}
                    {labels.length > 0 ? (
                      <span
                        style={{ position: "absolute", width: 1, height: 1, overflow: "hidden", clip: "rect(0 0 0 0)" }}
                      >
                        {labels.join(", ")}
                      </span>
                    ) : null}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
