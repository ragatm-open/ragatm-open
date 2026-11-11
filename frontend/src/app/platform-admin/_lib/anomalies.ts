// Codes d'anomalie owner-scope (§18 : jamais de code brut à l'UI).
// Les codes viennent du diagnostic backend (source de vérité). Chaque code est
// mappé vers une clé i18n du namespace « platformAdmin » ; la traduction se fait
// au rendu (t(anomalyLabelKey(code))).

export const ANOMALY_LABEL_KEYS: Record<string, string> = {
  workspace_disabled: "anomalyWorkspaceDisabled",
  workspace_without_owner: "anomalyWorkspaceWithoutOwner",
  multiple_owners: "anomalyMultipleOwners",
  inactive_owner: "anomalyInactiveOwner",
  non_owner_without_scope: "anomalyNonOwnerWithoutScope",
  ragspace_without_scope: "anomalyRagspaceWithoutScope",
  scope_target_not_owner: "anomalyScopeTargetNotOwner",
  scope_cross_workspace: "anomalyScopeCrossWorkspace",
  access_scope_mismatch: "anomalyAccessScopeMismatch",
  multiple_active_accesses: "anomalyMultipleActiveAccesses",
};

// integrity = incohérence bloquante ; decision = choix humain requis ; info = attendu.
export const ANOMALY_TONE: Record<string, "integrity" | "decision" | "info"> = {
  scope_cross_workspace: "integrity",
  scope_target_not_owner: "integrity",
  access_scope_mismatch: "integrity",
  multiple_active_accesses: "integrity",
  workspace_without_owner: "decision",
  multiple_owners: "decision",
  workspace_disabled: "info",
  inactive_owner: "info",
  non_owner_without_scope: "info",
  ragspace_without_scope: "info",
};

// Renvoie la clé i18n pour un code, ou "" si le code est inconnu
// (dans ce cas, l'appelant affiche le code brut en repli).
export function anomalyLabelKey(code: string): string {
  return ANOMALY_LABEL_KEYS[code] || "";
}
