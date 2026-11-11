"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { apiFetch } from "@/lib/api";

type ValidateInfo = {
  status: "pending" | "expired" | "accepted" | "revoked";
  workspace_name: string;
  role: string;
  email_masked: string;
  account_active: boolean;
};

type Phase =
  | "loading"
  | "invalid"
  | "expired"
  | "revoked"
  | "used"
  | "new" // compte nouveau/inactif → saisir mot de passe
  | "active" // compte actif → se connecter pour accepter
  | "success";

const card: React.CSSProperties = {
  maxWidth: 460,
  margin: "48px auto",
  padding: 28,
  border: "1px solid #e6e0d8",
  borderRadius: 16,
  background: "#fff",
  fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
  color: "#2a2320",
};
const inputStyle: React.CSSProperties = {
  padding: 11,
  border: "1px solid #ddd6cd",
  borderRadius: 9,
  fontSize: 14,
  width: "100%",
  boxSizing: "border-box",
};
const btn: React.CSSProperties = {
  padding: "11px 16px",
  borderRadius: 9,
  border: "none",
  background: "#C2562F",
  color: "#fff",
  fontWeight: 600,
  fontSize: 14,
  cursor: "pointer",
};

export default function AcceptInvitationClient() {
  const router = useRouter();
  const sp = useSearchParams();
  const t = useTranslations("invitations");
  // Capture le token une seule fois puis nettoie l'URL (réduit l'exposition via
  // historique navigateur / Referer). Le token vit ensuite en mémoire uniquement.
  const [token] = useState(() => sp.get("token") || "");
  useEffect(() => {
    if (token && typeof window !== "undefined") {
      window.history.replaceState({}, "", "/accept-invitation");
    }
  }, [token]);

  const [phase, setPhase] = useState<Phase>("loading");
  const [info, setInfo] = useState<ValidateInfo | null>(null);
  const [pw1, setPw1] = useState("");
  const [pw2, setPw2] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const validate = useCallback(async () => {
    if (!token) {
      setPhase("invalid");
      return;
    }
    try {
      const data = await apiFetch<ValidateInfo>("/api/v1/invitations/validate/", {
        method: "POST",
        body: JSON.stringify({ token }),
      });
      setInfo(data);
      if (data.status === "expired") setPhase("expired");
      else if (data.status === "revoked") setPhase("revoked");
      else if (data.status === "accepted") setPhase("used");
      else setPhase(data.account_active ? "active" : "new");
    } catch {
      setPhase("invalid");
    }
  }, [token]);

  useEffect(() => {
    validate();
  }, [validate]);

  async function submitNew(e: React.FormEvent) {
    e.preventDefault();
    setMsg(null);
    if (!pw1 || !pw2) return setMsg(t("missingPasswords"));
    if (pw1 !== pw2) return setMsg(t("passwordMismatch"));
    setBusy(true);
    try {
      await apiFetch("/api/v1/invitations/accept/", {
        method: "POST",
        body: JSON.stringify({ token, password: pw1, password_confirmation: pw2 }),
      });
      setPhase("success");
      setTimeout(() => router.push("/login"), 1200);
    } catch (err: any) {
      setMsg(err?.message?.replace(/^API \d+[^:]*:\s*/, "") || t("acceptFailed"));
    } finally {
      setBusy(false);
    }
  }

  async function acceptAuthenticated() {
    setMsg(null);
    setBusy(true);
    try {
      await apiFetch("/api/v1/invitations/accept-authenticated/", {
        method: "POST",
        body: JSON.stringify({ token }),
      });
      setPhase("success");
      setTimeout(() => router.push("/chat"), 1000);
    } catch (err: any) {
      const m = err?.message || "";
      if (/401|403|email_mismatch|Authentication/i.test(m)) {
        setMsg(t("signInMismatch"));
      } else {
        setMsg(m.replace(/^API \d+[^:]*:\s*/, "") || t("acceptFailed"));
      }
    } finally {
      setBusy(false);
    }
  }

  const roleKey = info
    ? { admin: "roleAdmin", member: "roleMember", viewer: "roleViewer", owner: "roleOwner" }[info.role]
    : undefined;
  const roleLabel = roleKey ? t(roleKey) : info?.role || "";

  if (phase === "loading") return <div style={card}>{t("validatingLink")}</div>;

  if (phase === "invalid")
    return (
      <div style={card}>
        <h1 style={{ fontSize: 20, marginTop: 0 }}>{t("invalidTitle")}</h1>
        <p style={{ color: "#6b6259" }}>{t("invalidBody")}</p>
      </div>
    );

  if (phase === "expired" || phase === "revoked" || phase === "used") {
    const texts = {
      expired: [t("expiredTitle"), t("expiredBody")],
      revoked: [t("revokedTitle"), t("revokedBody")],
      used: [t("usedTitle"), t("usedBody")],
    }[phase];
    return (
      <div style={card}>
        <h1 style={{ fontSize: 20, marginTop: 0 }}>{texts[0]}</h1>
        <p style={{ color: "#6b6259" }}>{texts[1]}</p>
        <a href="/login" style={{ ...btn, display: "inline-block", textDecoration: "none", marginTop: 8 }}>
          {t("goToLogin")}
        </a>
      </div>
    );
  }

  if (phase === "success")
    return (
      <div style={card}>
        <h1 style={{ fontSize: 20, marginTop: 0 }}>{t("successTitle")}</h1>
        <p style={{ color: "#6b6259" }}>{t("successBody")}</p>
      </div>
    );

  // En-tête commun aux deux flux actifs (new / active)
  const header = (
    <>
      <h1 style={{ fontSize: 20, marginTop: 0, marginBottom: 6 }}>
        {t("joinTitle", { workspace: info?.workspace_name || "" })}
      </h1>
      <p style={{ color: "#6b6259", marginTop: 0, fontSize: 13.5 }}>
        {t("invitedForRole", { email: info?.email_masked || "", role: roleLabel })}
      </p>
    </>
  );

  if (phase === "active")
    return (
      <div style={card}>
        {header}
        <p style={{ color: "#2a2320", fontSize: 14, lineHeight: 1.5 }}>{t("activeAccountInfo")}</p>
        <div style={{ display: "flex", gap: 10, marginTop: 16, flexWrap: "wrap" }}>
          <button style={btn} disabled={busy} onClick={acceptAuthenticated}>
            {busy ? t("working") : t("acceptSignedIn")}
          </button>
          <a
            href="/login"
            style={{ ...btn, background: "#efe9e1", color: "#2a2320", textDecoration: "none", display: "inline-block" }}
          >
            {t("signIn")}
          </a>
        </div>
        {msg && <div style={{ marginTop: 14, color: "#8A2E20", fontSize: 13 }}>{msg}</div>}
      </div>
    );

  // phase === "new"
  return (
    <div style={card}>
      {header}
      <p style={{ color: "#2a2320", fontSize: 14, lineHeight: 1.5 }}>{t("choosePassword")}</p>
      <form onSubmit={submitNew} style={{ display: "grid", gap: 12, marginTop: 8 }}>
        <label style={{ display: "grid", gap: 6, fontSize: 13 }}>
          <span>{t("newPassword")}</span>
          <input type="password" value={pw1} onChange={(e) => setPw1(e.target.value)} autoComplete="new-password" style={inputStyle} />
        </label>
        <label style={{ display: "grid", gap: 6, fontSize: 13 }}>
          <span>{t("confirmPassword")}</span>
          <input type="password" value={pw2} onChange={(e) => setPw2(e.target.value)} autoComplete="new-password" style={inputStyle} />
        </label>
        <div style={{ fontSize: 12, color: "#8a8078" }}>{t("passwordHint")}</div>
        <button type="submit" style={{ ...btn, opacity: busy ? 0.7 : 1 }} disabled={busy}>
          {busy ? t("activating") : t("activateAndJoin")}
        </button>
        {msg && <div style={{ color: "#8A2E20", fontSize: 13 }}>{msg}</div>}
      </form>
    </div>
  );
}
