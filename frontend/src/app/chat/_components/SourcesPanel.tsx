"use client";

/**
 * Panneau de sources — LE seul rendu de source de l'application.
 *
 * Extrait de `page.tsx` au lot E3 pour être testable en isolation. Ce n'est PAS un
 * composant Excel dédié : c'est le même panneau, qui sait désormais afficher une
 * feuille et une plage quand la source en porte — et qui n'affiche rien de plus
 * quand elle n'en porte pas.
 *
 * Toutes les métadonnées passent par les helpers de `_lib/sources` : aucun composant
 * ne lit `source.meta` directement (les anciens messages n'en ont pas, et un cast
 * local finirait par diverger).
 */

import React, { useEffect } from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { Icon, Pill } from "@/components/ui";
import type { SummaryScope } from "@/lib/api";
import type { Msg } from "../_lib/types";
import {
  srcAudioTimeRange,
  srcBadgeLabel,
  srcCellRange,
  srcCorpusName,
  srcDoc,
  srcDocId,
  srcIsSpeakerNotes,
  srcEmailAttachment,
  srcEmailSubject,
  srcLocatorLabel,
  srcPage,
  srcSheet,
  srcSheetHidden,
  srcSlide,
  srcSlideTitle,
  srcReference,
  srcSnippet,
  srcTitle,
  srcType,
  srcViewerHref,
  type AnySource,
  type SourceReference,
} from "../_lib/sources";
import styles from "./SourcesPanel.module.css";

/** Cible de `aria-controls` du bouton Sources de la Topbar. */
export const SOURCES_PANEL_ID = "chat-sources";

type SourcesPanelProps = {
  msg: Msg | null;
  activeN: number | null;
  onCite: (n: number) => void;
  /**
   * Reçoit une référence DISCRIMINÉE, pas la source brute : le panneau est le
   * dernier endroit qui connaît la provenance, et c'est elle — pas l'UUID — qui
   * détermine l'API à appeler. Passer la source entière laisserait l'appelant
   * refaire cette lecture, avec le risque de l'oublier (c'était le défaut).
   */
  onSummarize: (ref: SourceReference, scope: SummaryScope) => void;
  busy: boolean;
  /** Tiroir (tablette/mobile) ouvert. Sans effet en desktop. */
  open?: boolean;
  /** Ferme le tiroir ; le bouton n'est visible qu'en tablette/mobile. */
  onClose?: () => void;
  /** Fait défiler jusqu'à la source active (tiroir ouvert depuis une citation). */
  revealActive?: boolean;
};

/* ---------------- Panneau de sources ----------------
 * Desktop (≥ 1200 px) : colonne permanente de 340 px. Tablette/mobile : le MÊME
 * panneau devient un tiroir superposé, fermé par défaut (`open`), qui ne
 * réserve aucune largeur — cf. SourcesPanel.module.css. */
export const SourcesPanel = React.forwardRef<HTMLElement, SourcesPanelProps>(function SourcesPanel(
  { msg, activeN, onCite, onSummarize, busy, open = false, onClose, revealActive = false },
  ref
) {
  const t = useTranslations("chat");
  const sources = (msg?.sources || null) as AnySource[] | null;

  // Tiroir ouvert depuis une citation : amène la source choisie dans la vue.
  // Jamais en desktop, où la colonne garde son défilement tel quel.
  useEffect(() => {
    if (!revealActive || activeN == null) return;
    document.getElementById(`src-${activeN}`)?.scrollIntoView?.({ block: "nearest" });
  }, [revealActive, activeN, msg]);

  return (
    <aside
      ref={ref}
      id={SOURCES_PANEL_ID}
      aria-label={t("sourcesTitle")}
      className={styles.panel}
      data-open={open ? "true" : "false"}
    >
      <div className={styles.header} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 16 }}>
        <Icon name="doc" size={17} style={{ color: "var(--ink-2)" }} />
        <span style={{ fontSize: 14, fontWeight: 700, color: "var(--ink)" }}>{t("sourcesTitle")}</span>
        {sources && (
          <Pill color="var(--accent-ink)" bg="var(--accent-soft)">
            {sources.length}
          </Pill>
        )}
        {onClose && (
          <button type="button" className={styles.closeButton} onClick={onClose} aria-label={t("closeSources")}>
            <Icon name="close" size={20} />
          </button>
        )}
      </div>

      {!sources ? (
        <div style={{ fontSize: 13, color: "var(--ink-3)", lineHeight: 1.6, padding: "30px 4px", textAlign: "center" }}>
          <Icon name="search" size={26} style={{ color: "var(--ink-3)", opacity: 0.5, marginBottom: 10 }} />
          <div>{t("sourcesEmptyHint")}</div>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {sources.map((s, i) => {
            const n = i + 1;
            const active = activeN === n;
            const page = srcPage(s);
            const snippet = srcSnippet(s);
            const href = srcViewerHref(s);
            const docId = srcDocId(s);
            // Référence discriminée construite ICI, une fois : les deux actions
            // ci-dessous partagent la même, et aucune ne peut l'oublier.
            const ref = srcReference(s);
            // Citation classeur : feuille + plage. Absentes sur les anciens messages
            // et sur tout ce qui n'est pas un .xlsx -> rien ne s'affiche, rien ne casse.
            const sheet = srcSheet(s);
            const sheetHidden = srcSheetHidden(s);
            const range = srcCellRange(s);
            const title = srcTitle(s);
            // Citation présentation : diapositive + titre. Absentes sur tout ce qui
            // n'est pas un .pptx -> rien ne s'affiche, rien ne casse.
            const slide = srcSlide(s);
            const slideTitle = srcSlideTitle(s);
            const isNotes = srcIsSpeakerNotes(s);
            // Repère générique d'un document structuré : chemin et/ou
            // identifiant. Absent sur tout ce qui n'en porte pas -> rien ne s'affiche.
            // Transcription audio : l'intervalle horodaté remplace le
            // repère générique (même champ `locator`), libellé « Transcription audio ».
            const audioRange = srcAudioTimeRange(s);
            const audioLabel = audioRange
              ? `${t("audioTranscriptLabel", { range: audioRange.range })}${
                  audioRange.approximate ? ` ${t("audioTimestampApproximate")}` : ""
                }`
              : null;
            const locator = audioLabel ?? srcLocatorLabel(s);
            // Courriel : objet du message parent et pièce jointe citée.
            // Le repère interne (page, diapositive, feuille) reste celui de la pièce.
            const emailSubject = srcEmailSubject(s);
            const attachment = srcEmailAttachment(s);
            const a11y = [
              emailSubject ? t("a11yEmail", { subject: emailSubject }) : null,
              attachment ? t("a11yAttachment", { name: attachment.filename }) : null,
              t("a11ySource", { name: srcDoc(s) }),
              sheet ? t("a11ySheet", { sheet }) : null,
              range ? t("a11yCells", { range: range.replace(":", t("cellRangeSeparator")) }) : null,
              slide != null ? t("a11ySlide", { slide }) : null,
              slideTitle ? slideTitle : null,
              isNotes ? t("a11ySpeakerNotes") : null,
              locator ? locator : null,
              page != null ? t("a11yPage", { page }) : null,
            ]
              .filter(Boolean)
              .join(", ");
            return (
              <div
                key={i}
                id={`src-${n}`}
                onClick={() => onCite(n)}
                aria-label={a11y}
                style={{
                  padding: 14,
                  borderRadius: 12,
                  cursor: "pointer",
                  border: `1.5px solid ${active ? "var(--accent)" : "var(--border)"}`,
                  background: active ? "var(--accent-soft)" : "var(--surface)",
                  boxShadow: active ? "0 0 0 3px var(--accent-glow)" : "none",
                  transition: "border-color .15s, box-shadow .15s, background .15s",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
                  <span
                    style={{
                      minWidth: 20,
                      height: 20,
                      borderRadius: 6,
                      background: "var(--accent)",
                      color: "#fff",
                      display: "grid",
                      placeItems: "center",
                      fontSize: 11.5,
                      fontWeight: 700,
                      flex: "none",
                    }}
                  >
                    {n}
                  </span>
                  {/* Le nom du fichier peut être long : il se replie (`overflowWrap`)
                      plutôt que d'élargir le panneau ou de créer un scroll horizontal. */}
                  <span
                    style={{
                      fontSize: 12.5,
                      fontWeight: 600,
                      color: "var(--ink)",
                      lineHeight: 1.3,
                      flex: 1,
                      minWidth: 0,
                      overflowWrap: "anywhere",
                    }}
                  >
                    {srcDoc(s)}
                  </span>
                </div>
                {/* Courriel parent, puis pièce jointe : la hiérarchie de la citation. */}
                {emailSubject && (
                  <div style={{ fontSize: 12, color: "var(--ink-2)", marginBottom: 4, overflowWrap: "anywhere" }}>
                    {t("emailLabel", { subject: emailSubject })}
                  </div>
                )}
                {attachment && (
                  <div style={{ fontSize: 12, color: "var(--ink-2)", marginBottom: 7, overflowWrap: "anywhere" }}>
                    {t("attachmentLabel", { name: attachment.filename })}
                  </div>
                )}
                {/* Titre de la diapositive citée, quand elle en porte un. Rendu au
                    même endroit que le titre de tableau : c'est le même rôle — dire de
                    QUOI parle l'extrait avant de le lire. */}
                {slideTitle && (
                  <div
                    style={{
                      fontSize: 12,
                      color: "var(--ink-2)",
                      marginBottom: 7,
                      overflowWrap: "anywhere",
                    }}
                  >
                    {slideTitle}
                  </div>
                )}
                {/* Titre du tableau, quand l'auteur du classeur en a mis un. */}
                {title && (
                  <div
                    style={{
                      fontSize: 12,
                      color: "var(--ink-2)",
                      marginBottom: 7,
                      overflowWrap: "anywhere",
                    }}
                  >
                    {title}
                  </div>
                )}
                <div style={{ display: "flex", gap: 6, marginBottom: 9, flexWrap: "wrap" }}>
                  {/* Badge de provenance. Trois états EXCLUSIFS, jamais un défaut :
                      une provenance absente ou non reconnue est dite « inconnue », pas
                      présentée comme un document privé du workspace. */}
                  {srcType(s) === "domain_corpus" ? (
                    <Pill color="#7c3aed" bg="#f3e8ff">
                      {srcBadgeLabel(s)}
                      {srcCorpusName(s) ? ` · ${srcCorpusName(s)}` : ""}
                    </Pill>
                  ) : srcType(s) === "workspace_document" ? (
                    <Pill color="var(--accent-ink)" bg="var(--accent-soft)">
                      {t("workspaceBadge")}
                    </Pill>
                  ) : (
                    <Pill color="var(--ink-3)" bg="var(--chip)">
                      {t("unknownSourceBadge")}
                    </Pill>
                  )}
                  {/* Classeur : feuille + plage. Libellés explicites (« Feuille »,
                      « Cellules »), jamais les noms techniques du contrat. */}
                  {sheet && (
                    <Pill color="var(--ink-3)" bg="var(--chip)">
                      {t("sheetLabel", { sheet })}
                    </Pill>
                  )}
                  {/* Feuille masquée : dite en toutes lettres, jamais par la seule
                      couleur — le signal doit tenir sans la vue. */}
                  {sheet && sheetHidden && (
                    <Pill color="var(--ink-3)" bg="var(--chip)">
                      {t("sheetHidden")}
                    </Pill>
                  )}
                  {range && (
                    <Pill color="var(--ink-3)" bg="var(--chip)">
                      {t("cellsLabel", { range })}
                    </Pill>
                  )}
                  {/* Présentation : « Diapositive N », JAMAIS « p. N ». Le numéro vient
                      de meta.slide_number ; page_number est null pour un .pptx, donc la
                      pastille de page ci-dessous ne s'affiche pas — les deux rendus sont
                      mutuellement exclusifs par construction, pas par un if/else. */}
                  {slide != null && (
                    <Pill color="var(--ink-3)" bg="var(--chip)">
                      {t("slideLabel", { slide })}
                    </Pill>
                  )}
                  {/* Notes du présentateur : dit en toutes lettres. Une note est un
                      commentaire de l'orateur, pas le contenu publié de la diapositive. */}
                  {isNotes && (
                    <Pill color="var(--ink-3)" bg="var(--chip)">
                      {t("speakerNotes")}
                    </Pill>
                  )}
                  {/* Document structuré : repère « chemin · identifiant ». Une donnée rendue
                      en texte (échappement React), sans pastille de page : ces documents
                      n'ont pas de pages (page_number === null). */}
                  {locator && (
                    <Pill color="var(--ink-3)" bg="var(--chip)">
                      {locator}
                    </Pill>
                  )}
                  {/* PDF uniquement — un classeur et une présentation ont page_number === null. */}
                  {page != null && (
                    <Pill color="var(--ink-3)" bg="var(--chip)">
                      {t("pageShort", { page })}
                    </Pill>
                  )}
                </div>
                {snippet && (
                  <div
                    style={{
                      fontSize: 12.5,
                      color: "var(--ink-2)",
                      lineHeight: 1.55,
                      paddingLeft: 10,
                      borderLeft: "2px solid var(--accent)",
                      fontStyle: "italic",
                    }}
                  >
                    « {snippet} »
                  </div>
                )}
                {(href || docId) && (
                  <div style={{ marginTop: 11, display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center" }}>
                    {href && (
                      <Link
                        href={href}
                        onClick={(e) => e.stopPropagation()}
                        className={styles.action}
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 5,
                          color: "var(--accent-ink)",
                          fontSize: 12.5,
                          fontWeight: 600,
                          fontFamily: "inherit",
                          textDecoration: "none",
                        }}
                      >
                        {t("openDocument")} <Icon name="external" size={13} />
                      </Link>
                    )}
                    {ref && (
                      <SourceAction
                        icon="sparkles"
                        label={t("summarizeDocument")}
                        busy={busy}
                        onClick={(e) => {
                          e.stopPropagation();
                          onSummarize(ref, "document");
                        }}
                      />
                    )}
                    {ref && page != null && (
                      <SourceAction
                        icon="sparkles"
                        label={t("summarizePageAction", { page })}
                        busy={busy}
                        onClick={(e) => {
                          e.stopPropagation();
                          onSummarize(ref, "page");
                        }}
                      />
                    )}
                  </div>
                )}
              </div>
            );
          })}
          <div style={{ fontSize: 11.5, color: "var(--ink-3)", lineHeight: 1.5, marginTop: 4, padding: "0 2px" }}>
            {t("clickCitationHint")}
          </div>
        </div>
      )}
    </aside>
  );
});

/** Action secondaire d'une carte source (résumer le document / la page). */
function SourceAction({
  icon,
  label,
  busy,
  onClick,
}: {
  icon: string;
  label: string;
  busy: boolean;
  onClick: (e: React.MouseEvent) => void;
}) {
  const t = useTranslations("chat");
  return (
    <button
      type="button"
      disabled={busy}
      onClick={onClick}
      className={styles.action}
      title={busy ? t("summaryInProgress") : label}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        padding: 0,
        border: "none",
        background: "transparent",
        color: busy ? "var(--ink-3)" : "var(--accent-ink)",
        fontSize: 12.5,
        fontWeight: 600,
        fontFamily: "inherit",
        cursor: busy ? "default" : "pointer",
      }}
    >
      <Icon name={icon} size={13} />
      {label}
    </button>
  );
}
