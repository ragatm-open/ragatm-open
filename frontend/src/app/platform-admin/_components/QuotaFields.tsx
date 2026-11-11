"use client";

import React, { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Field } from "@/components/ui";
import {
  SIZE_UNITS,
  type SizeUnit,
  type QuotaUsage,
  unitToBytes,
  pickUnit,
  bytesToInputValue,
} from "@/lib/quota";
import { fileSize, formatCount } from "@/lib/format";

const unitSelectStyle: React.CSSProperties = {
  height: 46,
  padding: "0 11px",
  borderRadius: 10,
  border: "1.5px solid var(--border)",
  background: "var(--surface)",
  fontFamily: "inherit",
  fontSize: 14,
  color: "var(--ink)",
};

export type QuotaFieldsValue = { count: number; bytes: number; valid: boolean };

/**
 * Deux champs éditables (nombre de fichiers + taille avec sélecteur d'unité).
 * La valeur canonique remontée est TOUJOURS en octets ; le bouton parent se
 * désactive tant que `valid` est faux. Validation entier > 0, taille > 0, sous les
 * plafonds absolus.
 */
export function QuotaLimitsFields({
  initialCount,
  initialBytes,
  hardMaxCount,
  hardMaxBytes,
  onChange,
}: {
  initialCount: number;
  initialBytes: number;
  hardMaxCount: number;
  hardMaxBytes: number;
  onChange: (v: QuotaFieldsValue) => void;
}) {
  const t = useTranslations("platformAdmin");
  const initialUnit = pickUnit(initialBytes);
  const [countStr, setCountStr] = useState(String(initialCount));
  const [unit, setUnit] = useState<SizeUnit>(initialUnit);
  const [sizeStr, setSizeStr] = useState(bytesToInputValue(initialBytes, initialUnit));

  const count = Number(countStr);
  const sizeVal = Number(sizeStr);
  const bytes = unitToBytes(sizeVal, unit);
  const countValid =
    countStr.trim() !== "" && Number.isInteger(count) && count > 0 && count <= hardMaxCount;
  const sizeValid =
    sizeStr.trim() !== "" && Number.isFinite(sizeVal) && sizeVal > 0 && bytes >= 1 && bytes <= hardMaxBytes;
  const valid = countValid && sizeValid;

  // `onChange` via ref pour ne dépendre que des VALEURS (évite une boucle si le
  // parent passe une closure recréée à chaque rendu).
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  useEffect(() => {
    onChangeRef.current({ count, bytes, valid });
  }, [count, bytes, valid]);

  const countError =
    countStr.trim() !== "" && !countValid
      ? t("quotaCountInvalid", { max: formatCount(hardMaxCount) })
      : null;
  const sizeError =
    sizeStr.trim() !== "" && !sizeValid
      ? t("quotaSizeInvalid", { max: fileSize(hardMaxBytes) })
      : null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <Field
        id="quota-count"
        name="quota-count"
        type="number"
        inputMode="numeric"
        min={1}
        step={1}
        label={t("maxFilesLabel")}
        value={countStr}
        onChange={setCountStr}
        error={countError}
      />
      <div>
        <div style={{ fontSize: 13, fontWeight: 600, color: "var(--ink)", marginBottom: 7 }}>
          {t("maxSizeLabel")}
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
          <div style={{ flex: 1 }}>
            <Field
              id="quota-size"
              name="quota-size"
              type="number"
              inputMode="decimal"
              min={0}
              value={sizeStr}
              onChange={setSizeStr}
              error={sizeError}
            />
          </div>
          <select
            value={unit}
            onChange={(e) => setUnit(e.target.value as SizeUnit)}
            aria-label={t("sizeUnitLabel")}
            style={unitSelectStyle}
          >
            {SIZE_UNITS.map((u) => (
              <option key={u} value={u}>
                {t(`unit_${u}`)}
              </option>
            ))}
          </select>
        </div>
      </div>
    </div>
  );
}

function meterColor(percent: number, exceeded: boolean): string {
  if (exceeded || percent >= 95) return "var(--danger)";
  if (percent >= 80) return "var(--warning, #C8873B)";
  return "var(--accent)";
}

function MeterRow({
  label,
  valueText,
  percent,
  exceeded,
}: {
  label: string;
  valueText: string;
  percent: number;
  exceeded: boolean;
}) {
  const width = Math.max(0, Math.min(100, percent));
  const color = meterColor(percent, exceeded);
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12.5, marginBottom: 5 }}>
        <span style={{ color: "var(--ink-2)" }}>{label}</span>
        <strong style={{ color: exceeded ? "var(--danger)" : "var(--ink)" }}>{valueText}</strong>
      </div>
      <div
        style={{ height: 7, borderRadius: 4, background: "var(--chip, #EEE)", overflow: "hidden" }}
        role="progressbar"
        aria-valuenow={Math.round(width)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={label}
      >
        <div style={{ width: `${width}%`, height: "100%", background: color, transition: "width .2s" }} />
      </div>
    </div>
  );
}

/** Affichage lecture seule quota + consommation (fichiers + stockage). */
export function QuotaUsageMeter({ usage }: { usage: QuotaUsage }) {
  const t = useTranslations("platformAdmin");
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <MeterRow
        label={t("filesLabel")}
        valueText={`${formatCount(usage.used_document_count)} / ${formatCount(usage.max_document_count)}`}
        percent={usage.count_usage_percent}
        exceeded={usage.is_count_exceeded}
      />
      <MeterRow
        label={t("storageLabel")}
        valueText={`${fileSize(usage.used_document_bytes)} / ${fileSize(usage.max_document_bytes)}`}
        percent={usage.size_usage_percent}
        exceeded={usage.is_size_exceeded}
      />
      {usage.is_exceeded && (
        <div style={{ fontSize: 12, color: "var(--danger)" }}>{t("quotaExceededNotice")}</div>
      )}
    </div>
  );
}
