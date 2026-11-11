"use client";

// Frontière d'erreur globale (App Router) localisée. Client Component obligatoire
// pour ce fichier spécial Next ; traduction via useTranslations.
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui";

export default function GlobalError({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  const t = useTranslations("common");
  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 14,
        padding: 24,
        textAlign: "center",
        background: "var(--bg)",
        color: "var(--ink)",
      }}
    >
      <h1 style={{ fontSize: 22, fontWeight: 700, margin: 0 }}>{t("errorTitle")}</h1>
      <p style={{ color: "var(--ink-2)", margin: 0, maxWidth: "44ch" }}>{t("errorBody")}</p>
      <Button onClick={() => reset()}>{t("retry")}</Button>
    </div>
  );
}
