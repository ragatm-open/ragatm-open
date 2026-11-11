"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { apiFetchRaw } from "@/lib/api";
import { DocumentPreviewBlock } from "./DocumentPreviewBlock";
import { UnsupportedDocumentViewer } from "./UnsupportedDocumentViewer";
import {
  documentPreviewErrorKey,
  displayHeadingLevel,
  type DocumentPreview,
} from "../_lib/documentPreview";

type Status = "loading" | "ready" | "error";

/**
 * Visualiseur documentaire (DOCX) — aperçu structuré et borné.
 *
 * L'ouverture se fait sur le passage CITÉ : le frontend transmet le `chunk_id`,
 * et le backend convertit les offsets persistés (`char_start`/`char_end`) en
 * blocs et sous-plages. Le texte du snippet n'entre dans aucune décision.
 *
 * Cliquer un titre recharge une fenêtre bornée autour de son bloc, **sans**
 * `chunk_id` : on quitte alors la citation, et plus rien n'est surligné. Le
 * bouton « Revenir au passage cité » réinjecte le chunk.
 *
 * En cas d'échec, quel qu'il soit, on retombe sur l'état explicite — jamais sur
 * une zone blanche.
 */
export function DocumentPreviewViewer(props: {
  documentId: string;
  filename: string;
  chunkId: string | null;
  onUnauthorized: () => void;
  onDownload: () => void;
  onBack: () => void;
}) {
  const { documentId, filename, chunkId, onUnauthorized, onDownload, onBack } = props;
  const t = useTranslations("documentViewer");

  const [status, setStatus] = useState<Status>("loading");
  const [preview, setPreview] = useState<DocumentPreview | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  //  `null` = fenêtre pilotée par le chunk cité.
  const [anchorBlock, setAnchorBlock] = useState<number | null>(null);

  const cbRef = useRef({ onUnauthorized });
  useEffect(() => {
    cbRef.current = { onUnauthorized };
  }, [onUnauthorized]);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      setStatus("loading");
      setErrorCode(null);

      const qs = new URLSearchParams();
      if (chunkId && anchorBlock === null) qs.set("chunk_id", chunkId);
      if (anchorBlock !== null) qs.set("first_block", String(anchorBlock));

      try {
        // `apiFetchRaw` et non `apiGet` : ce dernier réduit le corps d'erreur à
        // son `detail` et PERD le `reason_code`, qui porte la raison exacte du
        // refus — « document réindexé » n'est pas « document illisible ».
        const res = await apiFetchRaw(
          `/api/v1/documents/${encodeURIComponent(documentId)}/document-preview/?${qs}`,
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
          return;
        }

        setPreview(body as DocumentPreview);
        setStatus("ready");
      } catch {
        if (cancelled) return;
        setErrorCode("network_error");
        setStatus("error");
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [documentId, chunkId, anchorBlock]);

  // Amène le passage cité dans le champ de vision : sans cela, l'utilisateur
  // arrive en haut de la fenêtre et doit chercher ce qu'il venait vérifier.
  useEffect(() => {
    if (status !== "ready") return;
    const el = document.getElementById("docx-source-anchor");
    el?.scrollIntoView({ block: "center", behavior: "auto" });
  }, [status, preview]);

  const goToHeading = useCallback((blockIndex: number) => {
    setAnchorBlock(blockIndex);
  }, []);

  const backToSource = useCallback(() => setAnchorBlock(null), []);

  if (status === "error") {
    return (
      <UnsupportedDocumentViewer
        filename={filename}
        reason={t(documentPreviewErrorKey(errorCode))}
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
        {t("loadingDocumentPreview")}
      </div>
    );
  }

  if (!preview) return null;

  const { blocks, navigation, viewport, truncation, fidelity } = preview;
  const sourceFirst = viewport.source_first_block;
  const showingSource = chunkId !== null && anchorBlock === null;

  if (viewport.total_blocks === 0) {
    return (
      <div
        style={{
          padding: 24,
          border: "1px solid #e5e7eb",
          borderRadius: 12,
          background: "white",
        }}
      >
        <h2 style={{ margin: "0 0 6px", fontSize: 16 }}>{t("noIndexableTextTitle")}</h2>
        <p style={{ margin: 0, fontSize: 14, opacity: 0.8 }}>
          {t("noIndexableTextBody")}
        </p>
        <button
          onClick={onDownload}
          style={{
            marginTop: 16,
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
      </div>
    );
  }

  return (
    <div style={{ display: "flex", gap: 16, alignItems: "flex-start" }}>
      <DocumentPreviewNavigation
        headings={navigation.headings}
        truncated={navigation.headings_truncated}
        onSelect={goToHeading}
        activeBlock={anchorBlock}
      />

      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 10 }}>
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
            {t("blocksRange", {
              first: viewport.first_block + 1,
              last: viewport.last_block + 1,
              total: viewport.total_blocks,
            })}
          </span>
          {showingSource && sourceFirst !== null ? (
            <span>
              {t("citedPassage")} :{" "}
              <strong
                style={{ background: "#fef3c7", padding: "1px 6px", borderRadius: 4 }}
              >
                {t("blockNumber", { number: sourceFirst + 1 })}
              </strong>
            </span>
          ) : chunkId ? (
            <button
              onClick={backToSource}
              style={{
                padding: "4px 10px",
                borderRadius: 999,
                border: "1px solid #111827",
                background: "white",
                cursor: "pointer",
                fontSize: 12,
              }}
            >
              ↩ {t("backToCitedPassage")}
            </button>
          ) : (
            <span style={{ opacity: 0.7 }}>{t("noCitationInWindow")}</span>
          )}
        </div>

        <div
          role="region"
          aria-label={t("documentContentAriaLabel", {
            filename: preview.document.filename,
          })}
          tabIndex={0}
          style={{
            border: "1px solid #e5e7eb",
            borderRadius: 12,
            background: "white",
            padding: "16px 20px",
            maxHeight: "calc(100vh - 300px)",
            overflowY: "auto",
            display: "flex",
            flexDirection: "column",
            gap: 10,
          }}
        >
          {blocks.map((block) => (
            <DocumentPreviewBlock
              key={block.id}
              block={block}
              isSourceAnchor={block.index === sourceFirst}
            />
          ))}
        </div>

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
            {t("truncatedDocument", { reason: truncation.reason ?? "" })}
          </div>
        ) : null}

        <DocumentPreviewFidelityNotice warnings={fidelity.warnings} />
      </div>
    </div>
  );
}

/** Sommaire — affiché seulement s'il existe, jamais reconstruit côté client. */
function DocumentPreviewNavigation(props: {
  headings: DocumentPreview["navigation"]["headings"];
  truncated: boolean;
  activeBlock: number | null;
  onSelect: (blockIndex: number) => void;
}) {
  const t = useTranslations("documentViewer");
  const { headings, truncated, activeBlock, onSelect } = props;

  if (headings.length === 0) {
    return (
      <nav
        aria-label={t("tocLabel")}
        style={{
          width: 240,
          flexShrink: 0,
          fontSize: 12,
          padding: 12,
          border: "1px solid #e5e7eb",
          borderRadius: 12,
          background: "white",
          opacity: 0.85,
        }}
      >
        {t("noReliableToc")}
      </nav>
    );
  }

  return (
    <nav
      aria-label={t("tocLabel")}
      style={{
        width: 240,
        flexShrink: 0,
        maxHeight: "calc(100vh - 300px)",
        overflowY: "auto",
        border: "1px solid #e5e7eb",
        borderRadius: 12,
        background: "white",
        padding: 8,
      }}
    >
      <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
        {headings.map((h) => {
          const level = displayHeadingLevel(h.level);
          return (
            <li key={h.id}>
              <button
                onClick={() => onSelect(h.block_index)}
                aria-current={activeBlock === h.block_index ? "true" : undefined}
                style={{
                  display: "block",
                  width: "100%",
                  textAlign: "left",
                  padding: "5px 8px",
                  paddingLeft: 8 + (level - 1) * 10,
                  border: "none",
                  borderRadius: 6,
                  background:
                    activeBlock === h.block_index ? "#111827" : "transparent",
                  color: activeBlock === h.block_index ? "white" : "#111827",
                  cursor: "pointer",
                  fontSize: 12.5,
                  fontWeight: level <= 2 ? 600 : 400,
                }}
                title={h.text}
              >
                {h.text}
              </button>
            </li>
          );
        })}
      </ul>
      {truncated ? (
        <div style={{ fontSize: 11, opacity: 0.7, padding: "6px 8px" }}>
          {t("tocTruncated")}
        </div>
      ) : null}
    </nav>
  );
}

/**
 * Bandeau de fidélité — **toujours affiché**.
 *
 * Un aperçu qui ne dit pas ce qu'il omet laisse croire qu'il montre tout le
 * fichier. Or il montre le contenu *indexé* : ni images, ni en-têtes, ni notes,
 * ni commentaires — que le pipeline ne lit pas.
 */
function DocumentPreviewFidelityNotice(props: { warnings: string[] }) {
  const t = useTranslations("documentViewer");
  const extra: string[] = [];
  if (props.warnings.includes("headings_not_detected")) {
    extra.push(t("fidelityDocxNoHeadings"));
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
      <strong>{t("simplifiedIndexedPreview")}</strong> {t("fidelityDocxBody")}
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
