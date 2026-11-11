"use client";

import { useTranslations } from "next-intl";

import type { PreviewSheet } from "../_lib/tabularPreview";

/**
 * Onglets de feuilles.
 *
 * La liste vient du backend et reflète la **politique d'ingestion**, pas le
 * contenu du fichier : les feuilles `veryHidden` n'y figurent pas, parce
 * qu'elles ne sont pas indexées. Ce composant n'a donc rien à filtrer — il rend
 * ce qu'il reçoit, et il ne doit surtout pas « compléter » la liste.
 *
 * La feuille porteuse de la citation est distinguée des autres : sans cela,
 * l'utilisateur qui navigue perd de vue d'où venait la réponse.
 */
export function SpreadsheetSheetTabs(props: {
  sheets: PreviewSheet[];
  activeIndex: number;
  onSelect: (index: number) => void;
  disabled?: boolean;
}) {
  const t = useTranslations("documentViewer");
  const { sheets, activeIndex, onSelect, disabled = false } = props;

  if (sheets.length <= 1) return null;

  return (
    <div
      role="tablist"
      aria-label={t("sheetsTablistLabel")}
      style={{
        display: "flex",
        gap: 6,
        overflowX: "auto",
        padding: "8px 0",
        borderBottom: "1px solid #e5e7eb",
      }}
    >
      {sheets.map((s) => {
        const active = s.index === activeIndex;
        return (
          <button
            key={s.index}
            role="tab"
            aria-selected={active}
            aria-label={
              s.has_citation
                ? t("sheetContainsSource", { name: s.name })
                : s.state === "hidden"
                  ? t("sheetHidden", { name: s.name })
                  : s.name
            }
            disabled={disabled}
            onClick={() => onSelect(s.index)}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              padding: "6px 12px",
              borderRadius: 999,
              border: active ? "1px solid #111827" : "1px solid #e5e7eb",
              background: active ? "#111827" : "white",
              color: active ? "white" : "#111827",
              cursor: disabled ? "wait" : "pointer",
              whiteSpace: "nowrap",
              flexShrink: 0,
              fontSize: 13,
              opacity: disabled && !active ? 0.6 : 1,
            }}
          >
            <span
              style={{
                maxWidth: 220,
                overflow: "hidden",
                textOverflow: "ellipsis",
              }}
              title={s.name}
            >
              {s.name}
            </span>

            {s.has_citation ? (
              <span
                title={t("containsCitedSource")}
                aria-hidden="true"
                style={{
                  fontSize: 10,
                  padding: "1px 6px",
                  borderRadius: 999,
                  background: active ? "#f59e0b" : "#fef3c7",
                  color: active ? "#111827" : "#92400e",
                  fontWeight: 700,
                }}
              >
                {t("sourceBadge")}
              </span>
            ) : null}

            {s.state === "hidden" ? (
              // Une feuille masquée est indexée — donc citable — mais elle
              // n'apparaît pas dans les onglets Excel de l'utilisateur. Taire
              // son état serait l'exposer sans le dire.
              <span
                title={t("hiddenSheetInExcel")}
                aria-hidden="true"
                style={{ fontSize: 11, opacity: 0.7 }}
              >
                🙈
              </span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}
