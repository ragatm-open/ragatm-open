"use client";

import React, { useMemo, useState } from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import type { DocumentCapabilitiesSnapshot } from "@/lib/coreConfig";
import { DEFAULT_FILTERS, filterFormats, filterMatrix, type FilterState } from "../_lib/capabilities";
import styles from "./DocumentCapabilities.module.css";
import { FormatDetail } from "./FormatDetail";
import { CapabilitiesHeader, StatusLegend } from "./HeaderAndLegend";
import { CapabilityFilters, CapabilityMatrix } from "./MatrixAndFilters";
import {
  AllFormatsSection,
  EmbeddingsSection,
  GatewayServicesSection,
  InlineImagesSection,
  RuntimeConsistencySection,
  SpaceFilterSection,
  WarningsSection,
} from "./Panels";
import { Section } from "./primitives";

/**
 * Vue PRÉSENTATIONNELLE de « Capacités documentaires ».
 * STRICTEMENT LECTURE SEULE : le seul déclencheur réseau est « Actualiser »
 * (et la vue consultative par espace), relayés au parent. Aucune activation,
 * modification, relance, réindexation ni import.
 */
export type DocumentCapabilitiesViewProps = {
  snapshot: DocumentCapabilitiesSnapshot | null;
  loading: boolean;
  refreshing: boolean;
  error: string | null;
  spaceError: string | null;
  onRefresh: () => void;
  onApplySpace: (ragspaceId: string) => void;
  onClearSpace: () => void;
};

function Skeleton() {
  const t = useTranslations("documentCapabilities");
  return (
    <div role="status" aria-live="polite" aria-busy="true" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <span style={{ fontSize: 13, color: "var(--ink-3)" }}>{t("loading")}</span>
      {[120, 260, 180].map((h, i) => (
        <div key={i} aria-hidden="true" style={{ height: h, borderRadius: 14, background: "var(--chip)", opacity: 0.6 }} />
      ))}
    </div>
  );
}

export function DocumentCapabilitiesView({
  snapshot,
  loading,
  refreshing,
  error,
  spaceError,
  onRefresh,
  onApplySpace,
  onClearSpace,
}: DocumentCapabilitiesViewProps) {
  const t = useTranslations("documentCapabilities");
  const [filters, setFilters] = useState<FilterState>(DEFAULT_FILTERS);
  const [selected, setSelected] = useState<string | null>(null);

  const formats = snapshot?.formats ?? [];
  const caps = snapshot?.capabilities ?? [];
  const matrixRows = useMemo(
    () => (snapshot ? filterMatrix(snapshot.matrix, formats, caps, filters) : []),
    [snapshot, formats, caps, filters]
  );
  const visibleFormats = useMemo(() => filterFormats(formats, caps, filters), [formats, caps, filters]);
  const selectedId = selected ?? matrixRows[0]?.format_id ?? null;
  const selectedFormat = formats.find((f) => f.format_id === selectedId) ?? null;

  return (
    <div className={styles.page}>
      <div>
        <Link href="/admin-core" style={{ fontSize: 13, color: "var(--accent-ink)" }}>
          ← {t("backToAdminCore")}
        </Link>
      </div>
      <CapabilitiesHeader snapshot={snapshot} onRefresh={onRefresh} refreshing={refreshing || loading} />

      {error && (
        <div
          role="alert"
          style={{ padding: "12px 14px", borderRadius: 10, background: "#FBE7E3", color: "#A12F20", fontSize: 13, border: "1px solid #F0C4BC" }}
        >
          {error}
        </div>
      )}

      {loading && !snapshot && <Skeleton />}

      {snapshot && (
        <>
          <StatusLegend />
          <SpaceFilterSection
            key={snapshot.ragspace_view?.id ?? "global"}
            activeSpace={snapshot.ragspace_view}
            error={spaceError}
            busy={refreshing || loading}
            onApply={onApplySpace}
            onClear={onClearSpace}
          />
          <WarningsSection warnings={snapshot.warnings ?? []} />
          <Section title={t("filtersTitle")} id="dc-filters">
            <CapabilityFilters filters={filters} onChange={setFilters} />
          </Section>
          <CapabilityMatrix rows={matrixRows} selected={selectedId} onSelect={setSelected} />
          <FormatDetail format={selectedFormat} capabilities={caps} showSpace={!!snapshot.ragspace_view} />
          <InlineImagesSection capabilities={caps} formats={formats} />
          <AllFormatsSection
            formats={visibleFormats}
            totalFormats={snapshot.summary.formats}
            totalExtensions={snapshot.summary.extensions}
            onSelect={setSelected}
          />
          <div className={styles.grid2}>
            <GatewayServicesSection services={snapshot.gateway_services ?? []} />
            <EmbeddingsSection embeddings={snapshot.embeddings} />
          </div>
          <RuntimeConsistencySection consistency={snapshot.runtime_consistency} />
        </>
      )}
    </div>
  );
}
