"use client";

import React, { forwardRef, useImperativeHandle, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui";
import styles from "./Composer.module.css";

/**
 * Longueur maximale d'une question, en caractères.
 *
 * Confort de saisie UNIQUEMENT : l'autorité est le backend, qui refuse en 400
 * au-delà (`MAX_QUESTION_CHARS`, backend/api/v1/limits.py — s'y trouvent la
 * mesure des usages réels et la raison du chiffre). Un `maxLength` côté client
 * évite à l'utilisateur de composer un texte qui serait rejeté ; il ne protège
 * rien, puisque l'API est appelable sans cette interface.
 */
export const MAX_QUESTION_CHARS = 4000;

/** Poignée du brouillon pour la page : la seule écriture dont elle a besoin. */
export type DraftComposerHandle = {
  /** Remplace le brouillon (vidage après envoi, suggestion recopiée…). */
  setValue: (v: string) => void;
};

type DraftComposerProps = Omit<React.ComponentProps<typeof Composer>, "input" | "setInput" | "onSend"> & {
  /** Reçoit le brouillon tel qu'affiché, au clic Envoyer ou sur Entrée. */
  onSend: (text: string) => void;
};

/**
 * Propriétaire du brouillon.
 *
 * Le texte saisi vit ICI, et non dans la page : un état de la page rerendait à
 * chaque caractère tout l'historique — chaque message, chaque rendu Markdown, le
 * panneau Sources — soit ~250 ms par frappe sur un fil de 450 messages. La page ne
 * lit le brouillon qu'à l'envoi (`onSend`) et ne l'écrit que par `setValue`.
 */
export const DraftComposer = forwardRef<DraftComposerHandle, DraftComposerProps>(function DraftComposer(
  { onSend, ...props },
  ref
) {
  const [draft, setDraft] = useState("");
  useImperativeHandle(ref, () => ({ setValue: setDraft }), []);
  return <Composer {...props} input={draft} setInput={setDraft} onSend={() => onSend(draft)} />;
});

export function Composer({
  input,
  setInput,
  onSend,
  onStop,
  loading,
  streaming,
  canAsk,
  placeholder,
}: {
  input: string;
  setInput: (v: string) => void;
  onSend: () => void;
  onStop: () => void;
  loading: boolean;
  streaming: boolean;
  canAsk: boolean;
  placeholder: string;
}) {
  const t = useTranslations("chat");
  const ref = useRef<HTMLTextAreaElement>(null);
  function onKey(e: React.KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      onSend();
    }
  }
  return (
    <div
      style={{
        border: "1.5px solid var(--border)",
        borderRadius: 16,
        background: "var(--surface)",
        padding: 10,
        boxShadow: "0 2px 10px rgba(60,40,25,.05)",
      }}
    >
      <textarea
        ref={ref}
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={onKey}
        rows={1}
        maxLength={MAX_QUESTION_CHARS}
        placeholder={placeholder}
        aria-label={t("yourQuestion")}
        className={styles.input}
        style={{
          width: "100%",
          border: "none",
          outline: "none",
          resize: "none",
          background: "transparent",
          fontFamily: "inherit",
          color: "var(--ink)",
          padding: "6px 8px",
          lineHeight: 1.5,
          maxHeight: 120,
        }}
        onInput={(e) => {
          const t = e.target as HTMLTextAreaElement;
          t.style.height = "auto";
          t.style.height = Math.min(t.scrollHeight, 120) + "px";
        }}
      />
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 4 }}>
        <div style={{ flex: 1 }} />
        {streaming && (
          <Button variant="secondary" size="sm" icon="stop" onClick={onStop} className={styles.action}>
            {t("stop")}
          </Button>
        )}
        <Button
          size="sm"
          icon="send"
          onClick={onSend}
          disabled={!canAsk || loading || !input.trim()}
          className={styles.action}
        >
          {t("send")}
        </Button>
      </div>
    </div>
  );
}
