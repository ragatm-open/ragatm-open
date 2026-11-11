"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { apiFetchRaw } from "@/lib/api";
import { PresentationBlock } from "./PresentationBlock";
import { UnsupportedDocumentViewer } from "./UnsupportedDocumentViewer";
import {
  hasVisualContent,
  presentationPreviewErrorKey,
  type PresentationPreview,
  type SlideView,
} from "../_lib/presentationPreview";

/** Libellé d'une diapositive : son titre, ou son numéro traduit à défaut. */
function useSlideLabel() {
  const t = useTranslations("documentViewer");
  return (slide: { number: number; title: string | null }) =>
    slide.title?.trim() || t("slideNumber", { number: slide.number });
}

type Status = "loading" | "ready" | "error";

/**
 * Visualiseur de présentation (PPTX) — navigation par diapositive.
 *
 * L'ouverture se fait sur la diapositive CITÉE : le frontend transmet le
 * `chunk_id`, et le backend résout la diapositive puis projette les offsets
 * persistés sur les blocs. Le texte du snippet n'entre dans aucune décision —
 * ce qui compte d'autant plus qu'un deck répète souvent le même intitulé.
 *
 * Naviguer vers une autre diapositive relance une requête bornée **sans**
 * `chunk_id` : plus rien n'est alors surligné, et « Revenir à la source »
 * réinjecte le chunk.
 */
export function PresentationPreviewViewer(props: {
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
  const [preview, setPreview] = useState<PresentationPreview | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  //  `null` = fenêtre pilotée par la citation.
  const [slide, setSlide] = useState<number | null>(null);

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
      if (chunkId && slide === null) qs.set("chunk_id", chunkId);
      if (slide !== null) qs.set("slide_number", String(slide));

      try {
        // `apiFetchRaw` et non `apiGet` : ce dernier réduit le corps d'erreur à
        // son `detail` et PERD le `reason_code`, qui porte la raison exacte —
        // « réglage des notes changé » n'est pas « présentation illisible ».
        const res = await apiFetchRaw(
          `/api/v1/documents/${encodeURIComponent(documentId)}/presentation-preview/?${qs}`,
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

        setPreview(body as PresentationPreview);
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
  }, [documentId, chunkId, slide]);

  // Amène le passage cité dans le champ de vision.
  useEffect(() => {
    if (status !== "ready") return;
    document.getElementById("pptx-source-anchor")?.scrollIntoView({
      block: "center",
      behavior: "auto",
    });
  }, [status, preview]);

  const goTo = useCallback(
    (n: number) => {
      const sourceSlide = preview?.viewport.source_slide;
      // Revenir sur la diapositive citée réactive le surlignage : on repasse en
      // mode « piloté par le chunk » plutôt que de figer un numéro.
      setSlide(chunkId && n === sourceSlide ? null : n);
    },
    [preview, chunkId]
  );

  const backToSource = useCallback(() => setSlide(null), []);

  if (status === "error") {
    return (
      <UnsupportedDocumentViewer
        filename={filename}
        reason={t(presentationPreviewErrorKey(errorCode))}
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
        {t("loadingPresentation")}
      </div>
    );
  }

  if (!preview) return null;

  const { presentation, viewport, slides, truncation, fidelity } = preview;
  const numbers = presentation.slides.map((s) => s.number);
  const pos = numbers.indexOf(viewport.active_slide);
  const prev = pos > 0 ? numbers[pos - 1] : null;
  const next = pos >= 0 && pos < numbers.length - 1 ? numbers[pos + 1] : null;
  const showingSource = chunkId !== null && slide === null;

  return (
    <div style={{ display: "flex", gap: 16, alignItems: "flex-start", flexWrap: "wrap" }}>
      <SlideNavigation
        slides={presentation.slides}
        activeSlide={viewport.active_slide}
        truncated={presentation.inventory_truncated}
        onSelect={goTo}
      />

      <div
        style={{
          flex: "1 1 420px",
          minWidth: 0,
          display: "flex",
          flexDirection: "column",
          gap: 10,
        }}
      >
        <div
          style={{
            display: "flex",
            gap: 10,
            flexWrap: "wrap",
            alignItems: "center",
            fontSize: 12,
          }}
        >
          <button
            onClick={() => prev !== null && goTo(prev)}
            disabled={prev === null || status === "loading"}
            style={navBtn(prev === null)}
          >
            ← {t("previous")}
          </button>
          <button
            onClick={() => next !== null && goTo(next)}
            disabled={next === null || status === "loading"}
            style={navBtn(next === null)}
          >
            {t("next")} →
          </button>
          <span style={{ opacity: 0.85 }}>
            {t("slideOf", {
              current: viewport.active_slide,
              total: presentation.slide_count,
            })}
          </span>
          {showingSource && viewport.source_slide !== null ? (
            <span
              style={{
                background: "#fef3c7",
                padding: "2px 8px",
                borderRadius: 999,
                fontSize: 11,
                fontWeight: 700,
                color: "#92400e",
              }}
            >
              {t("citedSource")}
            </span>
          ) : chunkId ? (
            <button onClick={backToSource} style={navBtn(false)}>
              ↩ {t("backToSource")}
            </button>
          ) : null}
        </div>

        {slides.map((s) => (
          <SlideCard key={s.number} slide={s} isActive={s.number === viewport.active_slide} />
        ))}

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

        <FidelityNotice warnings={fidelity.warnings} />
      </div>
    </div>
  );
}

function navBtn(disabled: boolean): React.CSSProperties {
  return {
    padding: "4px 10px",
    borderRadius: 999,
    border: "1px solid #e5e7eb",
    background: "white",
    cursor: disabled ? "not-allowed" : "pointer",
    opacity: disabled ? 0.5 : 1,
    fontSize: 12,
  };
}

/** Une diapositive : titre, blocs, puis notes — toujours séparées du corps. */
function SlideCard(props: { slide: SlideView; isActive: boolean }) {
  const t = useTranslations("documentViewer");
  const { slide, isActive } = props;
  const visual = hasVisualContent(slide);
  const empty = slide.blocks.length === 0;

  return (
    <section
      aria-label={`${t("slideNumber", { number: slide.number })}${
        slide.title ? ` — ${slide.title}` : ""
      }`}
      aria-current={isActive ? "true" : undefined}
      style={{
        border: isActive ? "1px solid #111827" : "1px solid #e5e7eb",
        borderRadius: 12,
        background: "white",
        padding: "14px 18px",
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
    >
      <div style={{ fontSize: 11, opacity: 0.6, fontWeight: 700 }}>
        {t("slideBadge", { number: slide.number })}
      </div>

      {empty ? (
        <p style={{ margin: 0, fontSize: 14, opacity: 0.8 }}>
          {t("slideNoIndexableText")}
        </p>
      ) : (
        slide.blocks.map((b) => <PresentationBlock key={b.id} block={b} />)
      )}

      {visual ? (
        <div
          style={{
            fontSize: 12,
            padding: "6px 10px",
            borderRadius: 8,
            background: "#eff6ff",
            border: "1px solid #bfdbfe",
            color: "#1e40af",
          }}
        >
          {t("slideHasVisuals")}
        </div>
      ) : null}

      {slide.speaker_notes.length > 0 ? (
        <div
          style={{
            marginTop: 4,
            paddingTop: 10,
            borderTop: "1px dashed #e5e7eb",
            display: "flex",
            flexDirection: "column",
            gap: 6,
          }}
        >
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <strong style={{ fontSize: 12 }}>{t("speakerNotes")}</strong>
            <span
              style={{
                fontSize: 10,
                padding: "1px 6px",
                borderRadius: 999,
                background: "#f3f4f6",
                color: "#374151",
              }}
            >
              {t("indexedSeparately")}
            </span>
          </div>
          {slide.speaker_notes.map((b) => (
            <PresentationBlock key={b.id} block={b} isNotes />
          ))}
        </div>
      ) : null}
    </section>
  );
}

/** Inventaire des diapositives — le backend l'a déjà borné et filtré. */
function SlideNavigation(props: {
  slides: PresentationPreview["presentation"]["slides"];
  activeSlide: number;
  truncated: boolean;
  onSelect: (n: number) => void;
}) {
  const t = useTranslations("documentViewer");
  const slideLabel = useSlideLabel();
  const { slides, activeSlide, truncated, onSelect } = props;

  return (
    <nav
      aria-label={t("slidesNavLabel")}
      style={{
        flex: "0 0 230px",
        maxHeight: "calc(100vh - 300px)",
        overflowY: "auto",
        border: "1px solid #e5e7eb",
        borderRadius: 12,
        background: "white",
        padding: 8,
      }}
    >
      <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
        {slides.map((s) => {
          const active = s.number === activeSlide;
          return (
            <li key={s.number}>
              <button
                onClick={() => onSelect(s.number)}
                aria-current={active ? "true" : undefined}
                aria-label={
                  s.has_citation
                    ? t("slideContainsSource", { label: slideLabel(s) })
                    : slideLabel(s)
                }
                style={{
                  display: "flex",
                  gap: 8,
                  alignItems: "center",
                  width: "100%",
                  textAlign: "left",
                  padding: "5px 8px",
                  border: "none",
                  borderRadius: 6,
                  background: active ? "#111827" : "transparent",
                  color: active ? "white" : "#111827",
                  cursor: "pointer",
                  fontSize: 12.5,
                }}
                title={slideLabel(s)}
              >
                <span style={{ opacity: 0.6, minWidth: 22 }}>{s.number}</span>
                <span
                  style={{
                    flex: 1,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {slideLabel(s)}
                </span>
                {s.has_citation ? (
                  <span
                    aria-hidden="true"
                    style={{
                      fontSize: 9,
                      padding: "1px 5px",
                      borderRadius: 999,
                      background: active ? "#f59e0b" : "#fef3c7",
                      color: active ? "#111827" : "#92400e",
                      fontWeight: 700,
                    }}
                  >
                    source
                  </span>
                ) : null}
              </button>
            </li>
          );
        })}
      </ul>
      {truncated ? (
        <div style={{ fontSize: 11, opacity: 0.7, padding: "6px 8px" }}>
          {t("inventoryTruncated")}
        </div>
      ) : null}
    </nav>
  );
}

/**
 * Bandeau de fidélité — **toujours affiché**.
 *
 * L'aperçu montre le contenu *indexé*, pas la présentation. Graphiques et
 * SmartArt sont muets à l'extraction et l'état masqué n'est pas lu : le taire
 * laisserait croire qu'une diapositive était vide alors qu'elle portait un
 * diagramme.
 */
function FidelityNotice(props: { warnings: string[] }) {
  const t = useTranslations("documentViewer");
  const extra: string[] = [];
  if (
    props.warnings.includes("charts_not_extracted") ||
    props.warnings.includes("smartart_not_extracted")
  ) {
    extra.push(t("fidelityPptxCharts"));
  }
  if (props.warnings.includes("hidden_slides_not_detected")) {
    extra.push(t("fidelityPptxHiddenSlides"));
  }
  if (props.warnings.includes("speaker_notes_included")) {
    extra.push(t("fidelityPptxNotes"));
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
      <strong>{t("simplifiedTextPreview")}</strong> {t("fidelityPptxBody")}
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
