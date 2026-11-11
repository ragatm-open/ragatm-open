"use client";

import React, { createContext, useContext, useMemo } from "react";
import { useTranslations } from "next-intl";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import styles from "./AssistantMarkdown.module.css";

/* Rendu du contenu assistant en Markdown GFM.
 *
 * Sécurité : le HTML brut éventuellement émis par le LLM n'est jamais interprété.
 * react-markdown ignore les noeuds HTML tant que rehype-raw est absent — cette
 * absence est volontaire (le contenu est influençable par les documents ingérés).
 */

/* ---------------- Colonnes fréquentes ---------------- */

/** Clés de colonnes reconnues pour l'ajustement visuel (jamais structurel). */
type ColKey = "date" | "source" | "page" | "reference" | null;

const ColumnsContext = createContext<ColKey[]>([]);

function normalizeLabel(label: string): string {
  return label
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .trim()
    .toLowerCase();
}

/** Reconnaît FR et EN : le tableau reste correct si rien n'est reconnu. */
function colKeyFromLabel(label: string): ColKey {
  const n = normalizeLabel(label);
  if (n.startsWith("date")) return "date";
  if (n.startsWith("source")) return "source";
  if (n.startsWith("page")) return "page";
  if (n.startsWith("reference") || n === "ref") return "reference";
  return null;
}

/** Texte concaténé d'un noeud hast. */
function nodeText(node: any): string {
  if (!node) return "";
  if (node.type === "text") return String(node.value ?? "");
  const children = Array.isArray(node.children) ? node.children : [];
  return children.map(nodeText).join("");
}

/** Extrait les clés de colonnes depuis le <thead> du noeud hast <table>. */
function headerKeys(tableNode: any): ColKey[] {
  const thead = (tableNode?.children ?? []).find((c: any) => c.tagName === "thead");
  const row = (thead?.children ?? []).find((c: any) => c.tagName === "tr");
  const cells = (row?.children ?? []).filter((c: any) => c.tagName === "th");
  return cells.map((c: any) => colKeyFromLabel(nodeText(c)));
}

/** Index d'une cellule dans sa ligne : injecté par le composant `tr`. */
type CellProps = React.HTMLAttributes<HTMLTableCellElement> & {
  node?: any;
  colIndex?: number;
};

function useColKey(colIndex?: number): ColKey {
  const cols = useContext(ColumnsContext);
  return typeof colIndex === "number" ? cols[colIndex] ?? null : null;
}

/* ---------------- Liens ---------------- */

/** Durcit le filtrage par défaut de react-markdown (ne le remplace pas). */
function safeUrlTransform(url: string): string {
  const safe = defaultUrlTransform(url);
  if (!safe) return "";
  const lowered = safe.trim().toLowerCase();
  if (lowered.startsWith("javascript:") || lowered.startsWith("data:") || lowered.startsWith("vbscript:")) {
    return "";
  }
  return safe;
}

function isExternal(href: string): boolean {
  return /^https?:\/\//i.test(href);
}

/* ---------------- Composant ---------------- */

/** Configuration statique : un tableau recréé à chaque rendu ne gagne rien. */
const REMARK_PLUGINS = [remarkGfm];

export type AssistantMarkdownProps = {
  content: string;
};

/**
 * Mémoïsé : le rendu re-parse TOUT le texte (micromark + GFM + mdast→hast→JSX),
 * pour un coût fixe par instance en plus du coût proportionnel au texte. Sans
 * cette frontière, tout rerendu de la page (citation, analyse, jeton de streaming)
 * re-parsait chaque réponse de l'historique. `content` est une chaîne : la
 * comparaison superficielle par défaut est exacte. La locale passe par le
 * contexte (`useTranslations`) et reste donc prise en compte.
 */
export const AssistantMarkdown = React.memo(function AssistantMarkdown({ content }: AssistantMarkdownProps) {
  const t = useTranslations("chat");
  const components = useMemo(
    () => ({
      // Les titres sont rendus DANS une bulle de conversation : sans échelle propre,
      // ils reprendraient les tailles par défaut du navigateur (h1 = 2em) et
      // écraseraient le fil. L'aide, qui en compte une douzaine, le rend visible.
      h1: ({ node, ...props }: any) => <h1 className={styles.h1} {...props} />,
      h2: ({ node, ...props }: any) => <h2 className={styles.h2} {...props} />,
      h3: ({ node, ...props }: any) => <h3 className={styles.h3} {...props} />,
      h4: ({ node, ...props }: any) => <h4 className={styles.h4} {...props} />,

      p: ({ node, ...props }: any) => <p className={styles.p} {...props} />,
      strong: ({ node, ...props }: any) => <strong className={styles.strong} {...props} />,
      em: ({ node, ...props }: any) => <em className={styles.em} {...props} />,
      ul: ({ node, ...props }: any) => <ul className={styles.ul} {...props} />,
      ol: ({ node, ...props }: any) => <ol className={styles.ol} {...props} />,
      li: ({ node, ...props }: any) => <li className={styles.li} {...props} />,
      blockquote: ({ node, ...props }: any) => <blockquote className={styles.blockquote} {...props} />,
      hr: ({ node, ...props }: any) => <hr className={styles.hr} {...props} />,

      // `code` porte toujours la classe inline ; la CSS la neutralise dans un `pre`.
      code: ({ node, className, ...props }: any) => <code className={styles.codeInline} {...props} />,
      pre: ({ node, ...props }: any) => <pre className={styles.pre} {...props} />,

      // Images non rendues : aucun chargement distant automatique depuis le LLM.
      img: ({ node, alt }: any) => <span>{alt || ""}</span>,

      a: ({ node, href, children, ...props }: any) => {
        const safe = typeof href === "string" ? href : "";
        if (!safe) {
          // Protocole neutralisé (javascript:, data:, ...) : texte non cliquable.
          return <span className={styles.unsafeLink}>{children}</span>;
        }
        const external = isExternal(safe);
        return (
          <a
            className={styles.a}
            href={safe}
            {...(external ? { target: "_blank", rel: "noopener noreferrer" } : {})}
            {...props}
          >
            {children}
          </a>
        );
      },

      table: ({ node, children, ...props }: any) => {
        const cols = headerKeys(node);
        return (
          <ColumnsContext.Provider value={cols}>
            <div className={styles.tableCard}>
              <div
                className={styles.tableScroll}
                role="region"
                aria-label={t("resultsTable")}
                tabIndex={0}
              >
                <table className={styles.table} {...props}>
                  {children}
                </table>
              </div>
            </div>
          </ColumnsContext.Provider>
        );
      },

      thead: ({ node, ...props }: any) => <thead className={styles.thead} {...props} />,
      tbody: ({ node, ...props }: any) => <tbody className={styles.tbody} {...props} />,

      // Injecte l'index de colonne dans les cellules pour l'ajustement visuel.
      tr: ({ node, children, ...props }: any) => {
        let i = 0;
        const cells = React.Children.map(children, (child) =>
          React.isValidElement(child)
            ? React.cloneElement(child as React.ReactElement<CellProps>, { colIndex: i++ })
            : child
        );
        return (
          <tr className={styles.tr} {...props}>
            {cells}
          </tr>
        );
      },

      th: ({ node, colIndex, ...props }: CellProps & { node?: any }) => {
        const col = useColKey(colIndex);
        return <th className={styles.th} scope="col" {...(col ? { "data-col": col } : {})} {...props} />;
      },

      td: ({ node, colIndex, ...props }: CellProps & { node?: any }) => {
        const col = useColKey(colIndex);
        return <td className={styles.td} {...(col ? { "data-col": col } : {})} {...props} />;
      },
    }),
    [t]
  );

  return (
    <div className={styles.root}>
      <ReactMarkdown remarkPlugins={REMARK_PLUGINS} urlTransform={safeUrlTransform} components={components as any}>
        {content}
      </ReactMarkdown>
    </div>
  );
});

export default AssistantMarkdown;
