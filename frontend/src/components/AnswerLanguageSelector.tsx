"use client";

// Sélecteur de LANGUE DE RÉPONSE IA (auto | fr | en) — DISTINCT du sélecteur de
// langue d'INTERFACE (LanguageSelector). Persiste la préférence du compte via
// PATCH /api/v1/me/preferences/. Accessible clavier + lecteurs d'écran (menu via
// ui/Popover portalisé). Dégradé sans blocage si le backend échoue.

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { Icon } from "@/components/ui";
import { Popover } from "@/components/ui/Popover";
import { getPreferences, updateAnswerLanguage, type AnswerLanguage } from "@/lib/api";

const OPTIONS: AnswerLanguage[] = ["auto", "fr", "en"];

/**
 * `onChange` remonte la valeur courante (au chargement ET à chaque choix). Le chat
 * s'en sert pour envoyer `answer_language` DANS la requête : sans cela, un changement
 * de préférence ne s'appliquerait qu'au tour suivant, une fois le PATCH de profil
 * arrivé en base. Optionnel : le composant reste utilisable seul.
 */
export function AnswerLanguageSelector({
  onChange,
}: {
  onChange?: (value: AnswerLanguage) => void;
} = {}) {
  const t = useTranslations("answerLanguage");
  const [value, setValue] = useState<AnswerLanguage>("auto");
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const anchorRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let alive = true;
    getPreferences()
      .then((p) => {
        if (alive && (p.preferred_answer_language === "fr" || p.preferred_answer_language === "en" || p.preferred_answer_language === "auto")) {
          setValue(p.preferred_answer_language);
          onChange?.(p.preferred_answer_language);
        }
      })
      .catch(() => {
        /* dégradé : conserve "auto" */
      });
    return () => {
      alive = false;
    };
    // `onChange` volontairement hors dépendances : une fonction recréée à chaque
    // rendu relancerait la lecture des préférences en boucle.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function pick(next: AnswerLanguage) {
    setOpen(false);
    anchorRef.current?.focus();
    if (next === value) return;
    const previous = value;
    setValue(next); // optimiste
    onChange?.(next);
    setBusy(true);
    try {
      await updateAnswerLanguage(next);
    } catch {
      setValue(previous); // rollback si échec backend
      onChange?.(previous);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={t("aria")}
        disabled={busy}
        title={t("label")}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          padding: "6px 10px",
          borderRadius: 10,
          border: "1px solid var(--line)",
          background: "var(--surface)",
          color: "var(--ink-2)",
          fontSize: 13,
          fontWeight: 600,
          cursor: "pointer",
        }}
      >
        <Icon name="chat" size={15} />
        <span>{t(value)}</span>
        <Icon name="chevronD" size={14} />
      </button>

      <Popover open={open} onClose={() => setOpen(false)} anchorRef={anchorRef} align="end" minWidth={200} role="listbox">
        {OPTIONS.map((opt) => {
          const selected = opt === value;
          return (
            <button
              key={opt}
              type="button"
              role="option"
              aria-selected={selected}
              onClick={() => pick(opt)}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: 10,
                width: "100%",
                padding: "9px 12px",
                border: "none",
                borderRadius: 8,
                background: selected ? "var(--surface-2)" : "transparent",
                color: "var(--ink)",
                fontSize: 13.5,
                fontWeight: selected ? 700 : 500,
                cursor: "pointer",
                textAlign: "left",
              }}
            >
              <span>
                {t(opt)}
                {opt === "auto" && (
                  <span style={{ display: "block", fontSize: 11.5, color: "var(--ink-3)", fontWeight: 500 }}>
                    {t("autoHint")}
                  </span>
                )}
              </span>
              {selected && <Icon name="check" size={16} />}
            </button>
          );
        })}
      </Popover>
    </>
  );
}
