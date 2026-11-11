# backend/apps/rag_core/services/ingest/docvision/license_gate.py
from __future__ import annotations

"""
Porte d'éligibilité licence du rail MinerU.

Ce module n'émet **aucun avis juridique** et n'essaie pas de déduire un statut
légal. Il encode une politique opérationnelle à partir d'une déclaration humaine
— le périmètre d'usage — et vérifie que la réalité technique du déploiement
n'est pas manifestement incompatible avec cette déclaration.

La logique précédente était un raccourci trompeur : `attribution_satisfied`
à `false` bloquait toujours, ce qui revenait à exiger une attribution même
lorsque l'obligation qui la commande ne s'applique pas. Le texte de la licence
MinerU 4.0.3 déclenche l'obligation sur un fait précis — *« if you provide online
services to third parties based on MinerU »* — et non sur l'installation du
moteur. C'est donc ce fait qu'il faut déclarer, pas l'attribution qu'il implique.

Le périmètre est une **déclaration humaine**, jamais une détection. Les invariants
ci-dessous ne classent personne : ce sont des garde-fous contre une configuration
qui contredirait visiblement la déclaration, par exemple un service déclaré
personnel mais ouvert anonymement.

Échec fermé partout : un périmètre absent, mal orthographié ou indécidable bloque.
"""

from dataclasses import dataclass, field
from typing import Dict, Optional

from . import config

#: Issues possibles. Les deux premières autorisent, les trois autres bloquent.
PASS_NOT_REQUIRED = "PASS_NOT_REQUIRED"
PASS_ATTRIBUTED = "PASS_ATTRIBUTED"
FAIL_ATTRIBUTION_REQUIRED = "FAIL_ATTRIBUTION_REQUIRED"
FAIL_SCOPE_UNKNOWN = "FAIL_SCOPE_UNKNOWN"
FAIL_SCOPE_INVARIANT = "FAIL_SCOPE_INVARIANT"

_AUTORISE = (PASS_NOT_REQUIRED, PASS_ATTRIBUTED)

PERSONAL_INTERNAL = "personal_internal"
THIRD_PARTY_SERVICE = "third_party_service"
UNKNOWN = "unknown"
PERIMETRES = (PERSONAL_INTERNAL, THIRD_PARTY_SERVICE, UNKNOWN)


@dataclass
class Decision:
    """Résultat de la porte. `eligible` ne dit rien du drapeau hybride."""

    status: str
    eligible: bool
    use_scope: str
    attribution_satisfied: bool
    invariants: Dict[str, bool] = field(default_factory=dict)
    failed_invariants: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "mineru_use_scope": self.use_scope,
            "mineru_attribution_satisfied": self.attribution_satisfied,
            "mineru_license_gate_status": self.status,
            "mineru_license_gate_reason": self.status,
            "mineru_license_gate_eligible": self.eligible,
            "mineru_license_gate_invariants": self.invariants,
            "mineru_license_gate_failed_invariants": self.failed_invariants,
        }


# ------------------------------------------------------------------ invariants
def _acces_anonyme_refuse() -> Optional[bool]:
    """L'API exige-t-elle une authentification par défaut ?

    Rend `None` si la question n'est pas décidable ; l'indécidable bloque.
    """
    try:
        from django.conf import settings

        classes = ((settings.REST_FRAMEWORK or {})
                   .get("DEFAULT_PERMISSION_CLASSES") or [])
        return any("IsAuthenticated" in str(c) for c in classes)
    except Exception:  # noqa: BLE001
        return None


def _inscription_publique_desactivee() -> Optional[bool]:
    """Un tiers peut-il se créer lui-même un compte utilisable ?

    On interroge le routeur plutôt qu'un réglage : c'est l'existence d'une route
    qui rend l'inscription possible, pas l'intention déclarée quelque part.
    """
    try:
        from django.urls import resolve
        from django.urls.exceptions import Resolver404

        for chemin in ("/api/v1/auth/register/", "/api/v1/auth/signup/",
                       "/api/v1/auth/registration/", "/register/", "/signup/"):
            try:
                resolve(chemin)
                return False
            except Resolver404:
                continue
        return True
    except Exception:  # noqa: BLE001
        return None


def _sidecar_non_public() -> Optional[bool]:
    """La cible sidecar configurée est-elle locale ou privée ?

    On réutilise le prédicat du client plutôt que d'en écrire un second : deux
    définitions du même contrôle finiraient par diverger.
    """
    try:
        from .client import _verifier_local

        _verifier_local(config.sidecar_url())
        return True
    except Exception as e:  # noqa: BLE001
        return False if e.__class__.__name__ == "SidecarRemoteRefused" else None


def _allowlist_sans_joker() -> Optional[bool]:
    try:
        return not any("*" in h for h in config.sidecar_hotes_autorises())
    except Exception:  # noqa: BLE001
        return None


#: Invariants exigés d'un périmètre déclaré personnel/interne. Chacun doit être
#: VRAI ; `None` (indécidable) vaut faux, par échec fermé.
INVARIANTS = {
    "anonymous_rag_denied": _acces_anonyme_refuse,
    "public_signup_disabled": _inscription_publique_desactivee,
    "sidecar_not_public": _sidecar_non_public,
    "allowlist_without_wildcard": _allowlist_sans_joker,
}


def evaluer_invariants() -> Dict[str, bool]:
    return {nom: bool(fn()) for nom, fn in INVARIANTS.items()}


# ----------------------------------------------------------------------- porte
def mineru_license_gate() -> Decision:
    """Décide si le rail MinerU est éligible au regard du périmètre déclaré."""
    scope = config.use_scope()
    attribue = config.attribution_satisfied()

    if scope == PERSONAL_INTERNAL:
        inv = evaluer_invariants()
        manquants = sorted(k for k, v in inv.items() if not v)
        if manquants:
            return Decision(FAIL_SCOPE_INVARIANT, False, scope, attribue, inv, manquants)
        return Decision(PASS_NOT_REQUIRED, True, scope, attribue, inv, [])

    if scope == THIRD_PARTY_SERVICE:
        if attribue:
            return Decision(PASS_ATTRIBUTED, True, scope, attribue)
        return Decision(FAIL_ATTRIBUTION_REQUIRED, False, scope, attribue)

    # Périmètre absent, vide ou non reconnu : on ne devine pas.
    return Decision(FAIL_SCOPE_UNKNOWN, False, scope, attribue)


def mineru_eligible() -> bool:
    """Raccourci booléen. Le drapeau hybride reste une condition SÉPARÉE."""
    return mineru_license_gate().status in _AUTORISE
