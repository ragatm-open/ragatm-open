"use client";

import React, { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { apiFetch } from "@/lib/api";
import { accentForId } from "@/lib/theme";
import { relativeTime, firstName } from "@/lib/format";
import { AppShell, Topbar, useWorkspace, type RagSpaceListItem, type RagSpaceStats } from "@/components/shell";
import { Button, Card, EmptyState, Icon, Pill, SectionLabel } from "@/components/ui";

export default function HomePage() {
  return (
    <AppShell>
      <HomeContent />
    </AppShell>
  );
}

function HomeContent() {
  const t = useTranslations("home");
  const router = useRouter();
  const { me, ragSpaces, canImport, setRagSpaceId, loading } = useWorkspace();

  const hour = new Date().getHours();
  const greet = hour < 12 ? t("greetingMorning") : hour < 18 ? t("greetingAfternoon") : t("greetingEvening");
  const prenom = firstName(me?.username || me?.email);

  function openSpace(id: string) {
    setRagSpaceId(id);
    router.push("/chat");
  }

  return (
    <>
      <Topbar title={t("title")} />
      <div style={{ padding: "28px 32px 56px", maxWidth: 1080, margin: "0 auto" }}>
        {/* Bienvenue + CTA */}
        <div
          style={{
            display: "flex",
            alignItems: "flex-end",
            justifyContent: "space-between",
            gap: 20,
            flexWrap: "wrap",
            marginBottom: 26,
          }}
        >
          <div>
            <div style={{ fontSize: 14, color: "var(--ink-3)", fontWeight: 500, marginBottom: 4 }}>
              {greet}{prenom ? `, ${prenom}` : ""} 👋
            </div>
            <h2
              style={{
                fontSize: 26,
                fontWeight: 700,
                letterSpacing: "-0.02em",
                color: "var(--ink)",
                margin: 0,
                maxWidth: 560,
                lineHeight: 1.2,
              }}
            >
              {t("question")}
            </h2>
          </div>
          <div style={{ display: "flex", gap: 10 }}>
            {canImport && (
              <Button variant="secondary" icon="upload" onClick={() => router.push("/documents")}>
                {t("import")}
              </Button>
            )}
            <Button icon="chat" onClick={() => router.push("/chat")}>
              {t("startChat")}
            </Button>
          </div>
        </div>

        {/* Raccourci barre de question */}
        <div
          onClick={() => router.push("/chat")}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") router.push("/chat");
          }}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 12,
            padding: "15px 18px",
            marginBottom: 34,
            borderRadius: 14,
            background: "var(--surface)",
            border: "1.5px solid var(--border)",
            cursor: "pointer",
            boxShadow: "var(--shadow-card)",
          }}
          onMouseEnter={(e) => (e.currentTarget.style.borderColor = "var(--accent)")}
          onMouseLeave={(e) => (e.currentTarget.style.borderColor = "var(--border)")}
        >
          <Icon name="sparkles" size={20} style={{ color: "var(--accent)" }} />
          <span style={{ flex: 1, fontSize: 15, color: "var(--ink-3)" }}>{t("askPlaceholder")}</span>
          <Pill color="var(--ink-3)" bg="var(--chip)">{t("enterKey")}</Pill>
        </div>

        {/* Espaces */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14 }}>
          <SectionLabel>{t("spacesLabel")}</SectionLabel>
          <span style={{ fontSize: 13, color: "var(--ink-3)" }}>
            {t("spacesCount", { count: ragSpaces.length })}
          </span>
        </div>

        {!loading && ragSpaces.length === 0 ? (
          <Card style={{ padding: 8 }}>
            <EmptyState
              icon="grid"
              title={t("noSpacesTitle")}
              body={t("noSpacesBody")}
            />
          </Card>
        ) : (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(248px, 1fr))",
              gap: 16,
            }}
          >
            {ragSpaces.map((s) => (
              <SpaceCard key={s.id} space={s} onOpen={() => openSpace(s.id)} />
            ))}
          </div>
        )}

        {/* Conseil */}
        <div style={{ marginTop: 38 }}>
          <SectionLabel>{t("tipsLabel")}</SectionLabel>
          <Card style={{ marginTop: 14, padding: 20, background: "var(--accent-soft)", border: "1px solid transparent", maxWidth: 520 }}>
            <Icon name="shield" size={22} style={{ color: "var(--accent)" }} />
            <div style={{ fontSize: 14.5, fontWeight: 600, color: "var(--accent-ink)", margin: "12px 0 6px" }}>
              {t("tipSourcedTitle")}
            </div>
            <p style={{ fontSize: 13.5, color: "var(--accent-ink)", opacity: 0.85, lineHeight: 1.55, margin: 0 }}>
              {t("tipSourcedBody")}
            </p>
          </Card>
        </div>
      </div>
    </>
  );
}

function SpaceCard({ space, onOpen }: { space: RagSpaceListItem; onOpen: () => void }) {
  const t = useTranslations("home");
  const [stats, setStats] = useState<RagSpaceStats | null>(null);
  const color = accentForId(space.id);

  useEffect(() => {
    let alive = true;
    apiFetch<RagSpaceStats>(`/api/v1/rag-spaces/${encodeURIComponent(space.id)}/stats/`)
      .then((s) => alive && setStats(s))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [space.id]);

  const total = stats?.documents.total ?? null;
  const ready = stats?.documents.ready ?? 0;
  // Les documents en échec sont aujourd'hui invisibles côté produit alors qu'ils
  // sont exclus de toute réponse : on les affiche dès qu'il y en a.
  const failed = stats?.documents.failed ?? 0;
  const pct = total ? Math.round((ready / Math.max(1, total)) * 100) : 0;

  return (
    <Card hover onClick={onOpen} style={{ padding: 18, display: "flex", flexDirection: "column", gap: 14 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <span style={{ width: 38, height: 38, borderRadius: 10, background: color, display: "grid", placeItems: "center" }}>
          <Icon name="grid" size={19} style={{ color: "#fff" }} />
        </span>
        <Icon name="arrowR" size={18} style={{ color: "var(--ink-3)" }} />
      </div>
      <div>
        <div style={{ fontSize: 15.5, fontWeight: 600, color: "var(--ink)", marginBottom: 4 }}>{space.name}</div>
        <div style={{ fontSize: 13, color: "var(--ink-2)", lineHeight: 1.5, minHeight: 38 }}>
          {space.llm_model ? t("modelLabel", { model: space.llm_model }) : t("spaceDefault")}
        </div>
      </div>
      {/* barre d'indexation : part des documents prêts */}
      <div style={{ height: 4, borderRadius: 999, background: "var(--chip)", overflow: "hidden" }}>
        <div style={{ width: `${pct}%`, height: "100%", background: color, opacity: 0.8 }} />
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 14, fontSize: 12.5, color: "var(--ink-3)" }}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
          <Icon name="doc" size={14} />
          {total != null ? total : "—"}
        </span>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
          <Icon name="grid" size={14} />
          {t("extractsCount", { count: stats?.chunks_total ?? 0 })}
        </span>
        {failed > 0 && (
          <span
            title={t("failedTitle", { count: failed })}
            style={{ display: "inline-flex", alignItems: "center", gap: 5, color: "var(--danger, #C2562F)" }}
          >
            <Icon name="alert" size={14} />
            {t("failedLabel", { count: failed })}
          </span>
        )}
        <span style={{ marginLeft: "auto", display: "inline-flex", alignItems: "center", gap: 5 }}>
          <Icon name="clock" size={14} />
          {relativeTime(stats?.last_created_at)}
        </span>
      </div>
    </Card>
  );
}
