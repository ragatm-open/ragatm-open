"use client";

import React from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui";
import { PAGE_SIZES } from "../_lib/documentsQuery";

/**
 * Pied de liste : plage affichée, taille de page, page précédente / suivante.
 *
 * La plage est écrite en clair (« 51–100 sur 237 ») plutôt qu'en numéro de page :
 * c'est la formulation qui répond à la question posée par l'écran — combien de
 * documents existent, et lesquels sont sous les yeux. C'est précisément ce que la
 * liste ne disait pas quand elle chargeait 200 lignes en les présentant comme le
 * tout.
 *
 * Même convention visuelle que la liste des workspaces de la console plateforme
 * (`WorkspacesTab`) : `limit`/`offset`, Précédent / Suivant, plage à côté.
 */
export function DocumentsPagination({
  total,
  limit,
  offset,
  loading,
  onOffset,
  onLimit,
}: {
  total: number;
  limit: number;
  offset: number;
  loading: boolean;
  onOffset: (offset: number) => void;
  onLimit: (limit: number) => void;
}) {
  const t = useTranslations("documents");
  const tc = useTranslations("common");

  const first = total === 0 ? 0 : offset + 1;
  const last = Math.min(offset + limit, total);
  const hasPrevious = offset > 0;
  const hasNext = offset + limit < total;

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 12,
        flexWrap: "wrap",
        padding: "12px 4px 0",
      }}
    >
      <span style={{ fontSize: 12.5, color: "var(--ink-3)" }}>
        {t("paginationRange", { first, last, total })}
      </span>

      <label style={{ display: "inline-flex", alignItems: "center", gap: 7, fontSize: 12.5, color: "var(--ink-3)" }}>
        {t("pageSizeLabel")}
        <select
          value={limit}
          disabled={loading}
          onChange={(e) => onLimit(Number(e.target.value))}
          style={{
            padding: "6px 8px",
            borderRadius: 8,
            border: "1px solid var(--border)",
            background: "var(--surface)",
            fontFamily: "inherit",
            fontSize: 12.5,
            color: "var(--ink)",
          }}
        >
          {PAGE_SIZES.map((n) => (
            <option key={n} value={n}>
              {n}
            </option>
          ))}
        </select>
      </label>

      <div style={{ flex: 1 }} />

      <Button
        size="sm"
        variant="secondary"
        disabled={!hasPrevious || loading}
        onClick={() => onOffset(Math.max(0, offset - limit))}
      >
        {tc("previous")}
      </Button>
      <Button
        size="sm"
        variant="secondary"
        disabled={!hasNext || loading}
        onClick={() => onOffset(offset + limit)}
      >
        {tc("next")}
      </Button>
    </div>
  );
}
