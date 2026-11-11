"use client";

import { useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { apiFetch } from "@/lib/api";

type ResetConfirmResponse =
  | {
      ok?: boolean;
      detail?: string;
      [k: string]: any;
    }
  | null;

function pickFirstError(res: any): string | null {
  if (!res || typeof res !== "object") return null;

  if (typeof res.detail === "string" && res.detail.trim()) return res.detail.trim();

  // DRF field errors often look like: { field: ["msg"] }
  for (const [k, v] of Object.entries(res)) {
    if (Array.isArray(v) && v.length > 0 && typeof v[0] === "string") {
      return `${k}: ${v[0]}`;
    }
    if (typeof v === "string" && v.trim()) {
      return `${k}: ${v.trim()}`;
    }
  }
  return null;
}

export default function ResetPasswordClient() {
  const router = useRouter();
  const sp = useSearchParams();
  const t = useTranslations("passwordReset");

  const uid = sp.get("uid") || "";
  const token = sp.get("token") || "";

  const [pw1, setPw1] = useState("");
  const [pw2, setPw2] = useState("");
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const ready = useMemo(() => Boolean(uid && token), [uid, token]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setMsg(null);

    if (!ready) {
      setMsg(t("invalidLinkDetail"));
      return;
    }
    if (!pw1 || !pw2) {
      setMsg(t("missingPasswords"));
      return;
    }
    if (pw1 !== pw2) {
      setMsg(t("passwordMismatch"));
      return;
    }

    setLoading(true);
    try {
      const payload = {
        uid,
        token,
        // ✅ attendu par l’API DRF (serializer): new_password
        new_password: pw1,
        // (optionnel) compat legacy si jamais tu choisis de supporter aussi côté backend
        new_password1: pw1,
        new_password2: pw2,
      };

      const res = await apiFetch<ResetConfirmResponse>("/api/v1/auth/password/reset/confirm/", {
        method: "POST",
        body: JSON.stringify(payload),
      });

      if (res?.ok) {
        setMsg(t("updated"));
        setTimeout(() => router.push("/login"), 600);
        return;
      }

      setMsg(pickFirstError(res) || t("updateFailed"));
    } catch (err: any) {
      // apiFetch peut throw en non-2xx ; on affiche un message lisible
      setMsg(err?.message || t("networkError"));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div style={{ maxWidth: 520, margin: "0 auto", padding: 16 }}>
      <h1 style={{ fontSize: 22, marginBottom: 8 }}>{t("title")}</h1>

      {!ready ? (
        <p style={{ opacity: 0.8 }}>{t("invalidLinkDetail")}</p>
      ) : (
        <form onSubmit={onSubmit} style={{ display: "grid", gap: 12 }}>
          <label style={{ display: "grid", gap: 6 }}>
            <span>{t("newPassword")}</span>
            <input
              type="password"
              value={pw1}
              onChange={(e) => setPw1(e.target.value)}
              autoComplete="new-password"
              style={{ padding: 10, border: "1px solid #ddd", borderRadius: 8 }}
            />
          </label>

          <label style={{ display: "grid", gap: 6 }}>
            <span>{t("confirmPassword")}</span>
            <input
              type="password"
              value={pw2}
              onChange={(e) => setPw2(e.target.value)}
              autoComplete="new-password"
              style={{ padding: 10, border: "1px solid #ddd", borderRadius: 8 }}
            />
          </label>

          <button
            type="submit"
            disabled={loading}
            style={{
              padding: 10,
              borderRadius: 8,
              border: "1px solid #ddd",
              cursor: loading ? "not-allowed" : "pointer",
            }}
          >
            {loading ? t("submitting") : t("submit")}
          </button>

          {msg ? <div style={{ padding: 10, border: "1px solid #eee", borderRadius: 8 }}>{msg}</div> : null}
        </form>
      )}
    </div>
  );
}
