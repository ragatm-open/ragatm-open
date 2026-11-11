"use client";

import { useTranslations } from "next-intl";

import {
  displayHeadingLevel,
  highlightExceedsDisplay,
  splitForHighlight,
  type DocumentBlock,
} from "../_lib/documentPreview";

/**
 * Rendu d'un bloc documentaire.
 *
 * Le contenu est du texte NON FIABLE : il est rendu en nœuds texte React, qui
 * échappe par construction. Aucun `dangerouslySetInnerHTML`, aucun lien activé,
 * aucune interprétation Markdown — un `<script>` s'affiche littéralement.
 *
 * Le surlignage est au CARACTÈRE, pas au bloc : les chunks sont découpés sans
 * égard pour les frontières de blocs, donc les blocs de bordure ne sont couverts
 * que partiellement.
 */
export function DocumentPreviewBlock(props: {
  block: DocumentBlock;
  isSourceAnchor?: boolean;
}) {
  const t = useTranslations("documentViewer");
  const { block, isSourceAnchor = false } = props;
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

  const anchorProps = isSourceAnchor
    ? { id: "docx-source-anchor", "data-source-anchor": "true" }
    : {};

  const common: React.CSSProperties = {
    // Préserve les retours à la ligne et tabulations produits par l'extracteur :
    // les collapser modifierait le texte affiché par rapport à l'indexé.
    whiteSpace: "pre-wrap",
    wordBreak: "break-word",
    margin: 0,
  };

  const truncationNote = block.truncated ? (
    <div style={{ fontSize: 11, opacity: 0.7, marginTop: 4 }}>
      {t("blockTruncatedChars", { count: block.full_length })}
      {highlightExceedsDisplay(block) ? ` ${t("highlightExceedsDisplayNote")}` : ""}
    </div>
  ) : null;

  if (block.type === "heading") {
    // Le niveau est borné 1..6 pour l'AFFICHAGE : l'extracteur peut renvoyer un
    // niveau hors plage (son heuristique lit le premier chiffre du style, donc
    // `Heading10` donne 1). On normalise le rendu sans toucher à la donnée.
    const level = displayHeadingLevel(block.level);
    const size = [22, 19, 17, 15, 14, 13][level - 1];
    const style: React.CSSProperties = {
      ...common,
      fontSize: size,
      fontWeight: 700,
      marginTop: 18,
    };
    return (
      <div {...anchorProps} data-block-type="heading" data-block-index={block.index}>
        {/* `role="heading"` + `aria-level` plutôt qu'un `h1..h6` dynamique :
            le niveau vient d'une heuristique, et un plan de titres faux serait
            annoncé comme structurel aux lecteurs d'écran. */}
        <div role="heading" aria-level={level} style={style}>
          {content}
        </div>
        {truncationNote}
      </div>
    );
  }

  if (block.type === "table") {
    return (
      <div {...anchorProps} data-block-type="table" data-block-index={block.index}>
        {/* Rendu PRÉFORMATÉ, jamais une grille : les fusions (gridSpan, vMerge)
            sont ignorées à l'extraction et une cellule vide en tête de ligne perd
            même sa position. Reconstruire un <table> placerait des valeurs dans
            les mauvaises colonnes, en silence. */}
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
          {t("tableSimplifiedDocx")}
        </div>
        {truncationNote}
      </div>
    );
  }

  if (block.type === "list") {
    return (
      <div {...anchorProps} data-block-type="list" data-block-index={block.index}>
        {/* Le pipeline agrège les items consécutifs en un seul bloc, avec une
            puce uniforme. On ne reconstruit pas de <ul>/<ol> : ni la numérotation,
            ni le niveau, ni la frontière entre deux listes ne sont connus. */}
        <div style={{ ...common, paddingLeft: 4 }}>{content}</div>
        <div style={{ fontSize: 11, opacity: 0.7, marginTop: 4 }}>
          {t("listSimplifiedDocx")}
        </div>
        {truncationNote}
      </div>
    );
  }

  return (
    <div {...anchorProps} data-block-type={block.type} data-block-index={block.index}>
      <p style={{ ...common, fontSize: 14.5, lineHeight: 1.6 }}>{content}</p>
      {truncationNote}
    </div>
  );
}
