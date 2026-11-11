"use client";

import { useTranslations } from "next-intl";

import {
  splitForHighlight,
  type PresentationBlock as Block,
} from "../_lib/presentationPreview";

/**
 * Rendu d'un bloc de diapositive.
 *
 * Le contenu est du texte NON FIABLE : rendu en nœuds texte React, qui échappe
 * par construction. Aucun `dangerouslySetInnerHTML`, aucun lien activé, aucune
 * liste ou table sémantique fabriquée — un `<script>` s'affiche littéralement.
 */
export function PresentationBlock(props: { block: Block; isNotes?: boolean }) {
  const t = useTranslations("documentViewer");
  const { block, isNotes = false } = props;
  const { before, marked, after } = splitForHighlight(block.text, block.highlight);

  const content = block.highlight ? (
    <>
      {before}
      <mark
        style={{
          background: "#fef3c7",
          boxShadow: "inset 0 -2px 0 #f59e0b",
          padding: "0 1px",
          borderRadius: 2,
        }}
      >
        {marked}
      </mark>
      {after}
    </>
  ) : (
    block.text
  );

  const common: React.CSSProperties = {
    // Préserve retours à la ligne et tabulations produits par l'extracteur :
    // les collapser modifierait le texte affiché par rapport à l'indexé.
    whiteSpace: "pre-wrap",
    wordBreak: "break-word",
    margin: 0,
  };

  const truncationNote = block.truncated ? (
    <div style={{ fontSize: 11, opacity: 0.7, marginTop: 4 }}>
      {t("blockTruncatedChars", { count: block.full_length })}
    </div>
  ) : null;

  const anchor = block.highlight
    ? { id: "pptx-source-anchor", "data-source-anchor": "true" }
    : {};

  if (block.type === "table") {
    return (
      <div {...anchor} data-block-type="table" data-block-index={block.index}>
        {/* Préformaté, jamais une grille : python-pptx RÉPÈTE la valeur d'une
            cellule fusionnée sur toute son étendue, et la matrice de cellules
            n'est nulle part persistée. Re-parser notre propre Markdown pour en
            refaire un tableau serait un aller-retour fragile. */}
        <pre
          style={{
            ...common,
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
            fontSize: 12.5,
            background: "#f9fafb",
            border: "1px solid #e5e7eb",
            borderRadius: 8,
            padding: "10px 12px",
            overflowX: "auto",
          }}
        >
          {content}
        </pre>
        <div style={{ fontSize: 11, opacity: 0.7, marginTop: 4 }}>
          {t("tableSimplifiedPptx")}
        </div>
        {truncationNote}
      </div>
    );
  }

  if (block.type === "title") {
    return (
      <div {...anchor} data-block-type="title" data-block-index={block.index}>
        {/* `role="heading"` plutôt qu'un `h1..h6` : le titre vient du placeholder
            canonique de PowerPoint, mais son NIVEAU dans un plan n'existe pas. */}
        <div
          role="heading"
          aria-level={2}
          style={{ ...common, fontSize: 18, fontWeight: 700 }}
        >
          {content}
        </div>
        {truncationNote}
      </div>
    );
  }

  if (block.type === "list") {
    return (
      <div {...anchor} data-block-type="list" data-block-index={block.index}>
        {/* Ni <ul> ni <ol> : la numérotation (`buAutoNum`) n'est pas lue à
            l'extraction, et les niveaux ne survivent que sous forme
            d'indentation. Fabriquer une liste sémantique affirmerait une
            structure que la donnée ne porte pas. */}
        <div style={{ ...common, fontSize: 14, lineHeight: 1.6 }}>{content}</div>
        <div style={{ fontSize: 11, opacity: 0.7, marginTop: 4 }}>
          {t("listSimplifiedPptx")}
        </div>
        {truncationNote}
      </div>
    );
  }

  const isMarker = block.warnings.includes("provenance_marker");
  const isAlt = block.warnings.includes("alt_text");

  return (
    <div
      {...anchor}
      data-block-type={isNotes ? "speaker_notes" : block.type}
      data-block-index={block.index}
    >
      <p
        style={{
          ...common,
          fontSize: isMarker ? 11.5 : 14,
          lineHeight: 1.6,
          // Le marqueur de provenance et le texte alternatif font partie du
          // texte INDEXÉ — donc citables, donc affichés — mais ils ne sont pas
          // du discours de l'auteur : le rendu les distingue sans les masquer.
          opacity: isMarker ? 0.55 : 1,
          fontStyle: isAlt ? "italic" : undefined,
        }}
      >
        {content}
      </p>
      {truncationNote}
    </div>
  );
}
