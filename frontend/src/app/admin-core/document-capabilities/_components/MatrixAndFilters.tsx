"use client";

import React from "react";
import { useTranslations } from "next-intl";
import type { MatrixColumn, MatrixRow } from "@/lib/coreConfig";
import { cellCounts, FAMILY_FILTERS, type FilterState } from "../_lib/capabilities";
import { capabilityStateMeta, formatStatusMeta } from "../_lib/status";
import styles from "./DocumentCapabilities.module.css";
import { Section, StatusPill, tdStyle, thStyle } from "./primitives";

export const MATRIX_COLUMNS: MatrixColumn[] = ["text", "ocr", "images", "vision", "tables", "charts", "audio"];

export function CapabilityFilters({ filters, onChange }: { filters: FilterState; onChange: (f: FilterState) => void }) {
  const t = useTranslations("documentCapabilities");
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div role="group" aria-label={t("familyFilterAria")} style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        {FAMILY_FILTERS.map((fam) => {
          const active = filters.family === fam;
          return (
            <button
              key={fam}
              type="button"
              aria-pressed={active}
              onClick={() => onChange({ ...filters, family: fam })}
              style={{
                padding: "5px 12px",
                borderRadius: 999,
                fontSize: 12.5,
                fontWeight: 600,
                fontFamily: "inherit",
                cursor: "pointer",
                border: `1px solid ${active ? "var(--accent-ink)" : "var(--border)"}`,
                background: active ? "var(--accent-soft)" : "var(--surface)",
                color: active ? "var(--accent-ink)" : "var(--ink-2)",
              }}
            >
              {t(`family_${fam}`)}
            </button>
          );
        })}
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "6px 18px", fontSize: 13, color: "var(--ink-2)" }}>
        <label style={{ display: "inline-flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={filters.hideUnsupported}
            onChange={(e) => onChange({ ...filters, hideUnsupported: e.target.checked })}
          />
          {t("hideUnsupported")}
        </label>
        <label style={{ display: "inline-flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={filters.searchableOnly}
            onChange={(e) => onChange({ ...filters, searchableOnly: e.target.checked })}
          />
          {t("searchableOnly")}
        </label>
      </div>
    </div>
  );
}

function Cell({ row, col }: { row: MatrixRow; col: MatrixColumn }) {
  const t = useTranslations("documentCapabilities");
  const cell = row.cells?.[col];
  if (!cell) return <StatusPill meta={capabilityStateMeta("NOT_APPLICABLE")} />;
  const { active, total } = cellCounts(cell.items ?? []);
  const meta = capabilityStateMeta(cell.state);
  const detail = (cell.items ?? []).map((i) => `${i.label} : ${t(capabilityStateMeta(i.state).key)}`).join("\n");
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-start", gap: 3 }} title={detail || undefined}>
      <StatusPill meta={meta} />
      {total > 0 && <span style={{ fontSize: 11.5, color: "var(--ink-3)" }}>{t("cellCount", { active, total })}</span>}
    </div>
  );
}

function GatewayCell({ row }: { row: MatrixRow }) {
  const t = useTranslations("documentCapabilities");
  const gw = row.cells?.gateway;
  const used = gw?.state === "USED";
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-start", gap: 3 }}>
      <StatusPill
        meta={used ? { tone: "info", icon: "globe", key: "gatewayUsed" } : { tone: "muted", icon: "dots", key: "gatewayNotUsed" }}
      />
      {used && (gw?.services?.length ?? 0) > 0 && (
        <span style={{ fontSize: 11.5, color: "var(--ink-3)" }}>{gw!.services.join(", ")}</span>
      )}
    </div>
  );
}

export function CapabilityMatrix({
  rows,
  selected,
  onSelect,
}: {
  rows: MatrixRow[];
  selected: string | null;
  onSelect: (formatId: string) => void;
}) {
  const t = useTranslations("documentCapabilities");
  const onKey = (e: React.KeyboardEvent, id: string) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onSelect(id);
    }
  };
  return (
    <Section title={t("matrixTitle")} id="dc-matrix" subtitle={t("matrixCaption")}>
      {rows.length === 0 ? (
        <div style={{ fontSize: 13, color: "var(--ink-3)" }}>{t("matrixEmpty")}</div>
      ) : (
        <>
          <div className={`${styles.tableWrap} ${styles.matrixDesktop}`} data-testid="dc-matrix-table">
            <table style={{ borderCollapse: "collapse", width: "100%", minWidth: 900 }}>
              <caption style={{ position: "absolute", width: 1, height: 1, overflow: "hidden", clip: "rect(0 0 0 0)" }}>
                {t("matrixCaption")}
              </caption>
              <thead>
                <tr>
                  <th scope="col" style={thStyle}>{t("colFormat")}</th>
                  {MATRIX_COLUMNS.map((c) => (
                    <th key={c} scope="col" style={thStyle}>{t(`col_${c}`)}</th>
                  ))}
                  <th scope="col" style={thStyle}>{t("col_gateway")}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => {
                  const isSel = selected === row.format_id;
                  return (
                    <tr
                      key={row.format_id}
                      className={styles.row}
                      tabIndex={0}
                      aria-selected={isSel}
                      aria-label={t("rowAria", { format: row.label })}
                      data-format-id={row.format_id}
                      onClick={() => onSelect(row.format_id)}
                      onKeyDown={(e) => onKey(e, row.format_id)}
                      style={{ cursor: "pointer", background: isSel ? "var(--accent-soft)" : undefined }}
                    >
                      <th scope="row" style={{ ...tdStyle, textAlign: "left", fontWeight: 700 }}>
                        <div style={{ display: "flex", flexDirection: "column", gap: 4, alignItems: "flex-start" }}>
                          <span>{row.label}</span>
                          <StatusPill meta={formatStatusMeta(row.format_status)} />
                        </div>
                      </th>
                      {MATRIX_COLUMNS.map((c) => (
                        <td key={c} style={tdStyle}>
                          <Cell row={row} col={c} />
                        </td>
                      ))}
                      <td style={tdStyle}>
                        <GatewayCell row={row} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <ul className={styles.cards} data-testid="dc-matrix-cards" style={{ listStyle: "none", padding: 0, margin: 0 }}>
            {rows.map((row) => {
              const isSel = selected === row.format_id;
              return (
                <li key={row.format_id}>
                  <div
                    role="button"
                    tabIndex={0}
                    aria-pressed={isSel}
                    aria-label={t("rowAria", { format: row.label })}
                    onClick={() => onSelect(row.format_id)}
                    onKeyDown={(e) => onKey(e, row.format_id)}
                    style={{
                      border: `1px solid ${isSel ? "var(--accent-ink)" : "var(--border)"}`,
                      borderRadius: 12,
                      padding: 12,
                      background: isSel ? "var(--accent-soft)" : "var(--surface)",
                      cursor: "pointer",
                    }}
                  >
                    <div style={{ display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
                      <strong style={{ fontSize: 14, color: "var(--ink)" }}>{row.label}</strong>
                      <StatusPill meta={formatStatusMeta(row.format_status)} />
                    </div>
                    <dl style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) auto", gap: "6px 10px", margin: 0 }}>
                      {MATRIX_COLUMNS.map((c) => (
                        <React.Fragment key={c}>
                          <dt style={{ fontSize: 12.5, color: "var(--ink-3)" }}>{t(`col_${c}`)}</dt>
                          <dd style={{ margin: 0 }}>
                            <Cell row={row} col={c} />
                          </dd>
                        </React.Fragment>
                      ))}
                      <dt style={{ fontSize: 12.5, color: "var(--ink-3)" }}>{t("col_gateway")}</dt>
                      <dd style={{ margin: 0 }}>
                        <GatewayCell row={row} />
                      </dd>
                    </dl>
                  </div>
                </li>
              );
            })}
          </ul>
        </>
      )}
    </Section>
  );
}
