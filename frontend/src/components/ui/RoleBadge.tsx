"use client";

import React from "react";
import { useTranslations } from "next-intl";
import { Icon } from "./Icon";
import { Pill } from "./StatusBadge";
import {
  getRagSpaceRoleLabelKey,
  type EffectiveRagSpaceAccess,
} from "@/lib/ragspacePermissions";

// Présentation du rôle EFFECTIF (décidé par le backend). Purement UX : ne décide
// aucun accès, se contente de traduire `permission_level` / owner-implicite.
const STYLE_BY_LEVEL: Record<string, { color: string; bg: string; icon: string }> = {
  manage: { color: "#9A3D17", bg: "#F6E7DE", icon: "shield" },
  write: { color: "#0E7C86", bg: "#DEF0F1", icon: "upload" },
  read: { color: "#5B53D9", bg: "#E7E5FA", icon: "eye" },
  none: { color: "#6B635B", bg: "#F0ECE7", icon: "lock" },
};

/**
 * Badge du rôle effectif d'un RagSpace. `access` provient du backend (jamais
 * recalculé). Un accès inconnu affiche « Accès restreint » (posture restrictive).
 */
export function RagSpaceRoleBadge({
  access,
  size = "md",
}: {
  access: EffectiveRagSpaceAccess | null | undefined;
  size?: "sm" | "md";
}) {
  const t = useTranslations("workspaces");
  const level = access?.permissionLevel ?? "none";
  const s = STYLE_BY_LEVEL[level] || STYLE_BY_LEVEL.none;
  const label = t(getRagSpaceRoleLabelKey(access));
  const fs = size === "sm" ? 11.5 : 12;
  return (
    <Pill color={s.color} bg={s.bg} style={{ fontSize: fs }}>
      <Icon name={s.icon} size={fs + 1} stroke={2} />
      {label}
    </Pill>
  );
}
