import Link from "next/link";
import { getTranslations } from "next-intl/server";

// Page 404 localisée (fr | en). Server Component : traduction via getTranslations.
export default async function NotFound() {
  const t = await getTranslations("common");
  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 12,
        padding: 24,
        textAlign: "center",
        background: "var(--bg)",
        color: "var(--ink)",
      }}
    >
      <div style={{ fontSize: 48, fontWeight: 800, color: "var(--ink-3)" }}>404</div>
      <h1 style={{ fontSize: 22, fontWeight: 700, margin: 0 }}>{t("notFoundTitle")}</h1>
      <p style={{ color: "var(--ink-2)", margin: 0, maxWidth: "42ch" }}>{t("notFoundBody")}</p>
      <Link
        href="/home"
        style={{ marginTop: 8, color: "var(--accent-ink)", fontWeight: 600, textDecoration: "none" }}
      >
        {t("backHome")}
      </Link>
    </div>
  );
}
