"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useTranslations } from "next-intl";
import {
  apiFetch,
  bootstrap,
  type AuthIdentity,
  type LoginStepResponse,
  type MfaSetupResponse,
  type MfaStatusResponse,
  type MfaStep,
} from "@/lib/api";
import { isApiError } from "@/lib/apiError";
import { Button, Field, Icon, Logo } from "@/components/ui";
import { LanguageSelector } from "@/components/LanguageSelector";
import PremiumRagPipelineHero from "@/components/login/PremiumRagPipelineHero";
import { setLocaleCookie } from "@/i18n/client";
import { coerceLocale } from "@/i18n/config";

// Parcours en deux facteurs (MFA obligatoire) :
//   credentials → mfa (code TOTP)            → bootstrap → /chat
//   credentials → mfa_setup (1er enrôlement) → bootstrap → /chat
// Le mot de passe seul ne pose AUCUN cookie d'auth : il ouvre un challenge serveur
// (cookie HttpOnly) que seuls les endpoints /api/v1/auth/mfa/* acceptent. Le
// bootstrap n'est appelé qu'après le second facteur. Rien n'est stocké côté
// navigateur : un rafraîchissement reprend l'étape via /mfa/status.
type Step = "credentials" | MfaStep;

export default function LoginPage() {
  const router = useRouter();
  const t = useTranslations("login");
  const tc = useTranslations("common");
  const ta = useTranslations("accessibility");

  const [step, setStep] = useState<Step>("credentials");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [setup, setSetup] = useState<MfaSetupResponse | null>(null);
  const [qrDataUrl, setQrDataUrl] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [show, setShow] = useState(false);
  const [loading, setLoading] = useState(false);

  // Rafraîchissement / retour arrière pendant le challenge : le serveur redonne
  // l'étape. Tout échec ici laisse simplement l'écran d'identifiants.
  useEffect(() => {
    let alive = true;
    apiFetch<MfaStatusResponse>("/api/v1/auth/mfa/status")
      .then((st) => {
        if (alive && st?.pending && st.next) setStep(st.next);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  // Premier enrôlement : l'URI d'appairage n'existe qu'en mémoire de cet écran.
  useEffect(() => {
    if (step !== "mfa_setup") {
      setSetup(null);
      setQrDataUrl(null);
      return;
    }
    let alive = true;
    apiFetch<MfaSetupResponse>("/api/v1/auth/mfa/setup", { method: "POST" })
      .then(async (data) => {
        if (!alive) return;
        setSetup(data);
        // Import dynamique : le générateur de QR ne pèse que sur cet écran. Le QR
        // est calculé dans le navigateur, l'URI n'est envoyée à aucun tiers.
        const QRCode = (await import("qrcode")).default;
        const url = await QRCode.toDataURL(data.provisioning_uri, { margin: 1, width: 220 });
        if (alive) setQrDataUrl(url);
      })
      .catch((ex) => alive && handleMfaError(ex));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step]);

  function rateLimitMessage(ex: { retryAfterMs: number | null }) {
    return ex.retryAfterMs != null
      ? t("rateLimited", { seconds: Math.max(1, Math.ceil(ex.retryAfterMs / 1000)) })
      : t("rateLimitedNoDelay");
  }

  function backToCredentials(message: string | null) {
    setStep("credentials");
    setCode("");
    setErr(message);
  }

  function handleMfaError(ex: any) {
    if (isApiError(ex)) {
      if (ex.status === 429) return setErr(rateLimitMessage(ex));
      if (ex.status === 400) return setErr(t("mfaInvalidCode"));
      // 401 : challenge expiré, consommé ou épuisé → nouveau mot de passe.
      if (ex.status === 401) return backToCredentials(t("mfaExpired"));
      // 409 : le compte n'a pas (ou plus) de facteur → enrôlement.
      if (ex.status === 409) {
        setCode("");
        setErr(null);
        return setStep("mfa_setup");
      }
      return backToCredentials(t("mfaRestart"));
    }
    setErr(t("networkError"));
  }

  // Authentification COMPLÈTE uniquement : le cookie d'auth vient d'être posé.
  async function finishLogin() {
    // Bootstrap (idempotent) — authentifié via le cookie déposé par le 2nd facteur.
    const boot = await bootstrap();

    // Synchronise le cookie de langue avec la préférence du compte (pas de second
    // appel réseau : la locale est déjà dans la réponse bootstrap).
    setLocaleCookie(coerceLocale(boot.preferred_locale));

    // Sélection par défaut (préférences UI non sensibles)
    localStorage.setItem("selectedWorkspaceId", String(boot.workspace.id));
    localStorage.setItem("selectedRagSpaceId", boot.ragspace.id);
    localStorage.setItem("bootstrapped", "1");

    router.replace("/chat");
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);

    if (!email || !password) {
      setErr(t("invalidCredentials"));
      return;
    }

    setLoading(true);
    try {
      // 1er facteur — pose le cookie de challenge (HttpOnly) + le cookie CSRF.
      const res = await apiFetch<LoginStepResponse>("/api/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      // Le mot de passe n'a plus d'usage : il ne reste pas en mémoire de la page.
      setPassword("");
      setCode("");
      setStep(res?.next === "mfa_setup" ? "mfa_setup" : "mfa");
    } catch (ex: any) {
      // 429 : ni un mauvais mot de passe ni une panne : on dit d'attendre, avec le
      // délai du serveur s'il est connu. Aucun réessai automatique ; champs conservés.
      if (isApiError(ex) && ex.status === 429) {
        setErr(rateLimitMessage(ex));
        return;
      }
      if (isApiError(ex) && ex.status === 403 && (ex.body as any)?.code === "account_locked") {
        setErr(t("accountLocked"));
        return;
      }
      const msg = String(ex?.message || "");
      // Erreurs d'authentification → message humain canonique ;
      // autres erreurs (réseau, serveur) → message technique pour le diagnostic.
      const isAuthError = /\b(400|401|403)\b/.test(msg);
      setErr(isAuthError || !msg ? t("invalidCredentials") : msg);
    } finally {
      setLoading(false);
    }
  }

  async function onSubmitCode(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    const token = code.replace(/\s+/g, "");
    if (!token) {
      setErr(t("mfaInvalidCode"));
      return;
    }

    setLoading(true);
    let verified = false;
    try {
      const path = step === "mfa_setup" ? "/api/v1/auth/mfa/setup/confirm" : "/api/v1/auth/mfa/verify";
      await apiFetch<AuthIdentity>(path, { method: "POST", body: JSON.stringify({ token }) });
      verified = true;
      setCode("");
      await finishLogin();
    } catch (ex: any) {
      if (!verified) {
        handleMfaError(ex);
      } else if (isApiError(ex) && ex.status === 429) {
        setErr(rateLimitMessage(ex));
      } else {
        setErr(String(ex?.message || t("networkError")));
      }
    } finally {
      setLoading(false);
    }
  }

  const heading = (
    <div style={{ marginBottom: 24 }}>
      <h1
        style={{
          fontSize: 25,
          fontWeight: 700,
          color: "var(--ink)",
          letterSpacing: "-0.02em",
          lineHeight: 1.25,
          margin: "0 0 10px",
        }}
      >
        {t("headlineLine1")}
        <br />
        {t("headlineLine2")}
        <br />
        {t("headlineLine3")}
      </h1>
      <p style={{ fontSize: 14.5, color: "var(--ink-2)", lineHeight: 1.55, margin: "0 0 8px", maxWidth: "42ch" }}>
        {t("description")}
      </p>
      <p style={{ fontSize: 13, color: "var(--ink-3)", lineHeight: 1.5, margin: 0, maxWidth: "44ch" }}>
        {t("corpora")}
      </p>
    </div>
  );

  return (
    <div
      className="login-grid"
      style={{ minHeight: "100vh", display: "grid", gridTemplateColumns: "1fr 1fr", background: "var(--bg)" }}
    >
      <style>{`
        @media (max-width: 860px) {
          .login-grid { grid-template-columns: 1fr !important; }
          .login-brand { display: none !important; }
          .login-form-col { padding: 32px 24px !important; }
        }
      `}</style>

      {/* Sélecteur de langue — coin supérieur droit, visible dès la connexion. */}
      <div style={{ position: "absolute", top: 16, right: 20, zIndex: 5 }}>
        <LanguageSelector variant="solid" />
      </div>

      {/* Colonne formulaire */}
      <div
        className="login-form-col"
        style={{
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          padding: "40px 48px",
        }}
      >
        {/* Bloc de marque — RAGatm Open */}
        <div>
          <div style={{ display: "flex", alignItems: "flex-end", gap: 8 }}>
            <Logo size={28} />
          </div>
          <p style={{ fontSize: 13.5, color: "var(--ink-2)", margin: "10px 0 0" }}>{tc("signature")}</p>
        </div>

        <div style={{ width: "100%", maxWidth: 380, margin: "0 auto" }}>
          {step === "credentials" && heading}

          {step === "credentials" ? (
            <form onSubmit={onSubmit} style={{ display: "flex", flexDirection: "column", gap: 16, width: "100%" }}>
              <Field
                label={t("emailLabel")}
                type="email"
                icon="mail"
                name="email"
                value={email}
                onChange={setEmail}
                placeholder={t("emailPlaceholder")}
                autoComplete="email"
                autoFocus
              />

              <Field
                label={t("passwordLabel")}
                type={show ? "text" : "password"}
                icon="lock"
                name="password"
                value={password}
                onChange={setPassword}
                placeholder="••••••••"
                autoComplete="current-password"
                error={err}
                right={
                  <button
                    type="button"
                    onClick={() => setShow((s) => !s)}
                    aria-label={show ? ta("hidePassword") : ta("showPassword")}
                    aria-pressed={show}
                    style={{
                      border: "none",
                      background: "transparent",
                      cursor: "pointer",
                      color: "var(--ink-3)",
                      padding: 4,
                      display: "grid",
                    }}
                  >
                    <Icon name="eye" size={18} />
                  </button>
                }
              />

              <div style={{ display: "flex", justifyContent: "flex-end", marginTop: -4 }}>
                <Link
                  href="/reset-password"
                  style={{ fontSize: 13, color: "var(--accent-ink)", fontWeight: 600, textDecoration: "none" }}
                >
                  {t("forgotPassword")}
                </Link>
              </div>

              <Button type="submit" size="lg" full disabled={loading}>
                {loading ? t("submitting") : t("submit")}
              </Button>

              <div style={{ textAlign: "center", fontSize: 13.5, color: "var(--ink-2)", marginTop: 2 }}>
                {t("demoPrompt")}{" "}
                {/* TODO: brancher sur le flux réel de demande de démonstration quand l'endpoint existera */}
                <a
                  href="mailto:contact@atelier-nord.fr?subject=Demande%20de%20d%C3%A9monstration%20RAGatm"
                  style={{ color: "var(--accent-ink)", fontWeight: 600, textDecoration: "none" }}
                >
                  {t("demoLink")}
                </a>
              </div>
            </form>
          ) : (
            <MfaForm
              step={step}
              code={code}
              setCode={setCode}
              err={err}
              loading={loading}
              setup={setup}
              qrDataUrl={qrDataUrl}
              onSubmit={onSubmitCode}
              onCancel={() => backToCredentials(null)}
            />
          )}
        </div>

        <div style={{ fontSize: 12.5, color: "var(--ink-3)", display: "flex", gap: 16 }}>
          <span>© {new Date().getFullYear()} RAGatm Open</span>
        </div>
      </div>

      <BrandPanel />
    </div>
  );
}

/* Second facteur — saisie du code (compte enrôlé) ou premier enrôlement TOTP. */
function MfaForm({
  step,
  code,
  setCode,
  err,
  loading,
  setup,
  qrDataUrl,
  onSubmit,
  onCancel,
}: {
  step: MfaStep;
  code: string;
  setCode: (v: string) => void;
  err: string | null;
  loading: boolean;
  setup: MfaSetupResponse | null;
  qrDataUrl: string | null;
  onSubmit: (e: React.FormEvent) => void;
  onCancel: () => void;
}) {
  const t = useTranslations("login");
  const enrolling = step === "mfa_setup";

  return (
    <form onSubmit={onSubmit} style={{ display: "flex", flexDirection: "column", gap: 16, width: "100%" }}>
      <div>
        <h1
          style={{
            fontSize: 23,
            fontWeight: 700,
            color: "var(--ink)",
            letterSpacing: "-0.02em",
            lineHeight: 1.25,
            margin: "0 0 10px",
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}
        >
          <Icon name="shield" size={22} /> {enrolling ? t("mfaSetupTitle") : t("mfaTitle")}
        </h1>
        <p style={{ fontSize: 14, color: "var(--ink-2)", lineHeight: 1.55, margin: 0 }}>
          {enrolling ? t("mfaSetupIntro") : t("mfaIntro")}
        </p>
      </div>

      {enrolling && (
        <ol style={{ margin: 0, paddingLeft: 20, display: "flex", flexDirection: "column", gap: 10, fontSize: 13.5, color: "var(--ink-2)", lineHeight: 1.5 }}>
          <li>{t("mfaSetupStepApp")}</li>
          <li>
            {t("mfaSetupStepScan")}
            <div style={{ marginTop: 10, display: "flex", flexDirection: "column", alignItems: "flex-start", gap: 10 }}>
              {qrDataUrl ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={qrDataUrl}
                  alt={t("mfaQrAlt")}
                  width={180}
                  height={180}
                  style={{ maxWidth: "100%", height: "auto", borderRadius: 8, border: "1px solid var(--border)", background: "#fff" }}
                />
              ) : (
                <div aria-live="polite" style={{ fontSize: 13, color: "var(--ink-3)" }}>
                  {t("mfaSetupLoading")}
                </div>
              )}
              {setup && (
                <div style={{ fontSize: 12.5, color: "var(--ink-3)" }}>
                  {t("mfaManualKey")}
                  <code
                    data-testid="mfa-manual-key"
                    style={{
                      display: "block",
                      marginTop: 4,
                      fontSize: 13,
                      color: "var(--ink)",
                      wordBreak: "break-all",
                      letterSpacing: "0.06em",
                    }}
                  >
                    {setup.secret.replace(/(.{4})/g, "$1 ").trim()}
                  </code>
                </div>
              )}
            </div>
          </li>
          <li>{t("mfaSetupStepCode")}</li>
        </ol>
      )}

      <Field
        label={t("mfaCodeLabel")}
        type="text"
        icon="lock"
        name="otp"
        value={code}
        onChange={setCode}
        placeholder="123456"
        autoComplete="one-time-code"
        inputMode="numeric"
        maxLength={16}
        autoFocus
        error={err}
        hint={enrolling ? undefined : t("mfaCodeHint")}
      />

      <Button type="submit" size="lg" full disabled={loading || (enrolling && !setup)}>
        {loading ? t("mfaSubmitting") : enrolling ? t("mfaSetupSubmit") : t("mfaSubmit")}
      </Button>

      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap", fontSize: 13 }}>
        <button
          type="button"
          onClick={onCancel}
          style={{ border: "none", background: "transparent", padding: 0, cursor: "pointer", color: "var(--accent-ink)", fontWeight: 600, fontSize: 13 }}
        >
          {t("mfaBack")}
        </button>
      </div>
      <p style={{ fontSize: 12.5, color: "var(--ink-3)", lineHeight: 1.5, margin: 0 }}>{t("mfaLostDevice")}</p>
    </form>
  );
}

/* Panneau de marque (colonne droite) — scène du pipeline RAG, purement illustrative. */
function BrandPanel() {
  return (
    <div
      className="login-brand"
      style={{
        position: "relative",
        overflow: "hidden",
        color: "#F7F2EB",
        background: "radial-gradient(120% 90% at 30% 35%, #3B291F 0%, #2C211B 48%, #241C18 100%)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "56px 48px 40px",
      }}
    >
      <PremiumRagPipelineHero />
    </div>
  );
}
