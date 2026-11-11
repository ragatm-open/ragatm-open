"use client";

import React from "react";
import { useTranslations } from "next-intl";
import { Button, Icon, Modal } from "@/components/ui";
import {
  type FolderFormatSummary,
  type FolderPlan,
  humanBytes,
  MAX_FOLDER_FILES,
  MAX_FOLDER_TOTAL_MB,
} from "../_lib/folderImport";

const REASON_LABEL_KEY: Record<string, string> = {
  office_temporary_file: "reasonOfficeTemp",
  unsupported: "reasonUnsupported",
  format_not_selected: "reasonNotSelected",
  too_large: "reasonTooLarge",
  empty: "reasonEmpty",
};

/**
 * Choix des formats + récapitulatif AVANT import : l'envoi ne démarre qu'après
 * confirmation, et ne porte que sur `plan.compatible` (déjà filtré par la
 * sélection). L'import est purement ADDITIF — aucun document existant n'est
 * touché, ce que le texte de la modale énonce explicitement.
 */
export function FolderImportModal({
  plan,
  formats,
  selected,
  onToggleFormat,
  maxSizeMb,
  onCancel,
  onConfirm,
}: {
  plan: FolderPlan;
  formats: readonly FolderFormatSummary[];
  selected: ReadonlySet<string>;
  onToggleFormat: (key: string) => void;
  maxSizeMb: number;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const t = useTranslations("documents");
  const officeTemp = plan.skipped.filter((s) => s.reason === "office_temporary_file").length;
  const unsupported = plan.skipped.filter((s) => s.reason === "unsupported").length;
  const notSelected = plan.skipped.filter((s) => s.reason === "format_not_selected").length;
  const tooLarge = plan.skipped.filter((s) => s.reason === "too_large").length;
  const empty = plan.skipped.filter((s) => s.reason === "empty").length;
  const noFormatSelected = selected.size === 0;
  const blocked = plan.compatible.length === 0 || plan.tooManyFiles || plan.tooLarge;
  const PREVIEW = 40;

  // Anti double-soumission : le premier clic verrouille le bouton, même si le
  // parent n'a pas encore démonté la modale.
  const [submitted, setSubmitted] = React.useState(false);
  function handleConfirm() {
    if (submitted || blocked) return;
    setSubmitted(true);
    onConfirm();
  }

  return (
    <Modal
      onClose={onCancel}
      title={t("importFolder")}
      subtitle={plan.sourceFolder ? t("folderSubtitle", { folder: plan.sourceFolder }) : t("folderSubtitleLocal")}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <p style={{ fontSize: 13, color: "var(--ink-2)", margin: 0 }}>
          {t("folderIntro")}{" "}
          <strong style={{ color: "var(--ink)" }}>{t("folderIntroKept")}</strong>
        </p>

        {/* Sélection des formats — une case par format fonctionnel. */}
        {formats.length > 0 && (
          <fieldset style={{ border: "1px solid var(--border)", borderRadius: 8, padding: "10px 12px", margin: 0 }}>
            <legend style={{ fontSize: 12, fontWeight: 700, color: "var(--ink)", padding: "0 4px" }}>
              {t("folderFormatsLegend")}
            </legend>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {formats.map(({ format, count }) => {
                const id = `fmt-${format.key}`;
                return (
                  <div key={format.key} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <input
                      type="checkbox"
                      id={id}
                      checked={selected.has(format.key)}
                      onChange={() => onToggleFormat(format.key)}
                      style={{ width: 15, height: 15, accentColor: "var(--accent)", flex: "none" }}
                    />
                    <label
                      htmlFor={id}
                      style={{
                        display: "flex",
                        alignItems: "baseline",
                        gap: 8,
                        flex: 1,
                        fontSize: 13,
                        color: "var(--ink)",
                        cursor: "pointer",
                      }}
                    >
                      <span style={{ fontWeight: 600 }}>{format.label}</span>
                      <span style={{ fontSize: 12, color: "var(--ink-3)" }}>
                        {t("filesCount", { count })}
                      </span>
                    </label>
                  </div>
                );
              })}
            </div>
          </fieldset>
        )}

        {/* Compteurs */}
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          <Stat label={t("statDetected")} value={plan.totalFound} />
          <Stat label={t("statToAdd")} value={plan.compatible.length} tone="ok" />
          {/* Ton « muted », jamais « warn » : un verrou Office n'est pas une anomalie
              de l'import, c'est un résidu normal de Word/Excel. Rien à corriger. */}
          {officeTemp > 0 && (
            <Stat label={t("statOfficeTemp")} value={officeTemp} tone="muted" />
          )}
          {notSelected > 0 && <Stat label={t("statNotSelected")} value={notSelected} tone="muted" />}
          {unsupported > 0 && <Stat label={t("statUnsupported")} value={unsupported} tone="muted" />}
          {tooLarge > 0 && <Stat label={t("statTooLarge")} value={tooLarge} tone="warn" />}
          {empty > 0 && <Stat label={t("statEmpty")} value={empty} tone="muted" />}
        </div>
        <div style={{ fontSize: 13, color: "var(--ink-2)" }}>
          <strong style={{ color: "var(--ink)" }}>{humanBytes(plan.totalBytes)}</strong> {t("folderToImport")}
          {" · "}{t("folderEachDistinct")}
        </div>

        {/* Aperçu des chemins relatifs retenus */}
        {plan.compatible.length > 0 && (
          <div>
            <div style={{ fontSize: 12, fontWeight: 700, color: "var(--ink)", marginBottom: 6 }}>
              {t("folderFilesToImport")}
            </div>
            <div
              style={{
                maxHeight: 180,
                overflow: "auto",
                border: "1px solid var(--border)",
                borderRadius: 8,
                padding: "6px 10px",
                fontSize: 12,
                color: "var(--ink-2)",
                fontFamily: "var(--mono, monospace)",
              }}
            >
              {plan.compatible.slice(0, PREVIEW).map((p, i) => (
                <div key={i} style={{ padding: "2px 0", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                  {p.relativePath}
                </div>
              ))}
              {plan.compatible.length > PREVIEW && (
                <div style={{ padding: "2px 0", color: "var(--ink-3)" }}>
                  {t("andMoreFiles", { count: plan.compatible.length - PREVIEW })}
                </div>
              )}
            </div>
          </div>
        )}

        {/* Fichiers écartés, avec le motif exact */}
        {plan.skipped.length > 0 && (
          <details style={{ fontSize: 12, color: "var(--ink-3)" }}>
            <summary style={{ cursor: "pointer" }}>{t("skippedFilesCount", { count: plan.skipped.length })}</summary>
            <div style={{ maxHeight: 120, overflow: "auto", marginTop: 6 }}>
              {plan.skipped.slice(0, PREVIEW).map((s, i) => (
                <div key={i} style={{ padding: "2px 0", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                  {s.relativePath || s.name} — {REASON_LABEL_KEY[s.reason] ? t(REASON_LABEL_KEY[s.reason]) : s.reason}
                </div>
              ))}
            </div>
          </details>
        )}

        {/* Garde-fous bloquants */}
        {plan.tooManyFiles && (
          <Note tone="warn">
            {t("folderTooManyFiles", { count: plan.compatible.length, max: MAX_FOLDER_FILES })}
          </Note>
        )}
        {plan.tooLarge && (
          <Note tone="warn">
            {t("folderTooLarge", { size: humanBytes(plan.totalBytes), max: MAX_FOLDER_TOTAL_MB })}
          </Note>
        )}
        {noFormatSelected && <Note tone="muted">{t("folderNoFormatSelected")}</Note>}
        {!noFormatSelected && plan.compatible.length === 0 && !plan.tooManyFiles && (
          <Note tone="muted">{t("folderNoCompatible", { mb: maxSizeMb })}</Note>
        )}

        <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 4 }}>
          <Button variant="secondary" onClick={onCancel}>
            {t("cancel")}
          </Button>
          <Button icon="upload" disabled={blocked || submitted} onClick={handleConfirm}>
            {t("importAndIndex")}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone?: "ok" | "warn" | "muted" }) {
  const color =
    tone === "ok" ? "var(--accent)" : tone === "warn" ? "#C2562F" : "var(--ink-3)";
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "baseline",
        gap: 5,
        padding: "4px 10px",
        borderRadius: 999,
        background: "var(--chip)",
        border: "1px solid var(--border)",
        fontSize: 12.5,
      }}
    >
      <strong style={{ color }}>{value}</strong>
      <span style={{ color: "var(--ink-3)" }}>{label}</span>
    </span>
  );
}

function Note({ tone, children }: { tone: "warn" | "muted"; children: React.ReactNode }) {
  return (
    <div
      style={{
        display: "flex",
        gap: 8,
        alignItems: "flex-start",
        fontSize: 12.5,
        color: tone === "warn" ? "#8a3a1c" : "var(--ink-3)",
        background: tone === "warn" ? "rgba(194,86,47,.08)" : "var(--chip)",
        border: "1px solid var(--border)",
        borderRadius: 8,
        padding: "8px 10px",
      }}
    >
      <Icon name="alert" size={14} style={{ marginTop: 1, flex: "none" }} />
      <span>{children}</span>
    </div>
  );
}
