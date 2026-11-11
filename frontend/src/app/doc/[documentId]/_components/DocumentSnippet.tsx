"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";

export function DocumentSnippet({ snippet }: { snippet: string }) {
  const t = useTranslations("documentViewer");
  const [copied, setCopied] = useState(false);

  async function onCopy() {
    try {
      await navigator.clipboard.writeText(snippet);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      // ignore
    }
  }

  return (
    <div
      style={{
        marginTop: 10,
        padding: 12,
        borderRadius: 12,
        border: "1px solid #e5e7eb",
        background: "#f9fafb",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", gap: 10, alignItems: "center" }}>
        <div style={{ fontWeight: 700 }}>{t("citedPassage")}</div>
        <button
          onClick={onCopy}
          style={{
            padding: "6px 10px",
            borderRadius: 10,
            border: "1px solid #e5e7eb",
            background: "white",
            cursor: "pointer",
            fontSize: 13,
          }}
        >
          {copied ? `${t("copied")} ✅` : t("copy")}
        </button>
      </div>
      <div style={{ marginTop: 8, whiteSpace: "pre-wrap", lineHeight: 1.5 }}>{snippet}</div>
    </div>
  );
}
