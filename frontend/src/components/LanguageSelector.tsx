"use client";

// Sélecteur de langue réutilisable (FR | EN). Accessible clavier + lecteurs
// d'écran (menu via ui/Popover portalisé, conforme à la doctrine « pas de
// position:absolute dans overflow:hidden »). Affiche la langue active, propose
// les libellés natifs (jamais de drapeaux seuls), met à jour cookie + profil et
// rafraîchit sans navigation complète.

import { useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { Icon } from "@/components/ui";
import { Popover } from "@/components/ui/Popover";
import { SUPPORTED_LOCALES, LOCALE_LABELS, type SupportedLocale } from "@/i18n/config";
import { useChangeLocale } from "@/i18n/client";

type Variant = "solid" | "ghost";

export function LanguageSelector({ variant = "ghost" }: { variant?: Variant }) {
  const t = useTranslations("accessibility");
  const { active, change, pending } = useChangeLocale();
  const [open, setOpen] = useState(false);
  const anchorRef = useRef<HTMLButtonElement>(null);

  function pick(locale: SupportedLocale) {
    change(locale);
    setOpen(false);
    anchorRef.current?.focus();
  }

  const border = variant === "solid" ? "1px solid var(--line)" : "1px solid transparent";
  const bg = variant === "solid" ? "var(--surface)" : "transparent";

  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={t("showLanguages")}
        disabled={pending}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          padding: "6px 10px",
          borderRadius: 10,
          border,
          background: bg,
          color: "var(--ink-2)",
          fontSize: 13,
          fontWeight: 600,
          cursor: "pointer",
        }}
      >
        <Icon name="globe" size={16} />
        <span>{active.toUpperCase()}</span>
        <Icon name="chevronD" size={14} />
      </button>

      <Popover open={open} onClose={() => setOpen(false)} anchorRef={anchorRef} align="end" minWidth={160} role="listbox">
        {SUPPORTED_LOCALES.map((locale) => {
          const selected = locale === active;
          return (
            <button
              key={locale}
              type="button"
              role="option"
              aria-selected={selected}
              onClick={() => pick(locale)}
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
                {LOCALE_LABELS[locale]} <span style={{ color: "var(--ink-3)", fontWeight: 500 }}>({locale.toUpperCase()})</span>
              </span>
              {selected && <Icon name="check" size={16} />}
            </button>
          );
        })}
      </Popover>
    </>
  );
}
