# Politique d'egress

**Statut : implémenté / activé par défaut** (invariant de sûreté, fail-closed).

Cette page décrit le contrôle qui refuse toute sortie de données hors de l'installation. Elle
complète [ai-gateway.md](ai-gateway.md) (transport) et [politiques-llm.md](politiques-llm.md)
(choix de provider/modèle).

> **RagATM Open n'a aucun provider externe.** La génération est servie par l'AI Gateway
> (modèle local) et les embeddings par le pipeline gouverné `ai_gateway`. Il n'existe donc
> aucun mécanisme d'ouverture (opt-in) de l'egress : la politique est un **invariant de
> sûreté** — aucune donnée ne quitte l'installation ; tout provider inconnu est refusé.

---

## 1. Invariant

> Aucun contenu utilisateur, document, chunk, résumé, historique ou métadonnée ne quitte
> l'installation. Tout provider qui n'est pas explicitement local est traité comme externe et
> **refusé**.

Le comportement est **fail-closed**, sans exception :

| Situation | Résultat |
|---|---|
| Contexte d'egress absent | **Refus** (`MissingEgressContext`) |
| Politique non résolue (périmètre inconnu) | **Refus** |
| Egress non autorisé | **Refus** (`ExternalEgressDenied`) |
| Provider hors liste autorisée | **Refus** (`ExternalProviderDenied`) |
| Provider inconnu / mal orthographié | Traité comme **externe** → refus |

Garde-fous de configuration, vérifiés au démarrage (system checks `llm_router`) :

* `AI_GATEWAY_FALLBACK_PROVIDER` n'accepte que `none` (défaut) ; `AI_GATEWAY_FALLBACK_ENABLED`
  vaut `0` par défaut (`llm_router.E004`) ;
* toute variable d'environnement d'un provider externe (clé, URL, modèle) est refusée (`config.E009`), et toute valeur de provider externe configurée l'est aussi (`llm_router.E009`) ;
* `EXTERNAL_EMBEDDINGS_EGRESS_ENABLED` existe toujours mais vaut toujours `0` : aucun provider
  d'embeddings externe ne peut être configuré.

## 2. Ordre de résolution

Du plus local au plus global — une politique locale ne peut jamais **élargir** une
interdiction supérieure : le global est un **plafond**, jamais un plancher. Le défaut du code est
**interdit**, et aucun provider externe n'existe pour que cette résolution aboutisse à une
autorisation.

`allow_external_fallback` (champ de RagSpace) est **conservé** mais n'a **aucun effet** : il n'y a
rien vers quoi se replier. Ses changements restent tracés dans `EgressPolicyChangeLog`.

## 3. Le contexte d'exécution

Tout appel LLM doit porter un `LlmExecutionContext` (`apps/llm_router/egress.py`), qui transporte
**ensemble** le périmètre (pour l'audit) et la décision (pour le contrôle).

| Fabrique | Usage |
|---|---|
| `LlmExecutionContext.for_rag_space(space, operation=…)` | Appel rattaché à un RagSpace. `space=None` ⇒ refus. |
| `LlmExecutionContext.system_local_only(operation=…)` | Tâche système **strictement locale**. Refus structurel de tout egress. |

## 4. Couverture des points d'appel

Tous les call-sites de génération sont gouvernés (`chat.answer`, `chat.answer.sse`,
`chat.answer.stream`, `chat.fallback`, `document.summary`, `chat.thread_memory`,
`analysis.global_search`, `actions.classify`, `chat.rephrasing_suggestions`, `query.rewrite`, …).
Aucun ne peut produire d'egress.

Les embeddings n'ont **aucun repli croisé** : une panne de Gateway lève une erreur, elle ne
bascule jamais vers un autre chemin.

### 4.1 Embeddings — *implémenté*

Les call-sites d'embeddings sont gouvernés par la **même** barrière que la génération
(`egress.enforce_external_egress`, avec `embedding=True`), **avant tout octet réseau**. Les
providers locaux (`ai_gateway`, `stub`) n'exigent aucun contexte : rien ne quitte
l'installation. Le corpus métier suit la même règle : provider `ai_gateway` uniquement
(`policy_source=domain-corpus-local-only`) ; l'ancienne exception qui lui permettait d'interroger
un provider externe n'existe plus.

## 5. Classification des erreurs Gateway

Sans repli externe, la classification sert à distinguer les défaillances transitoires (timeout,
connexion refusée, HTTP 500/502/503/504, 429) des erreurs de configuration ou de contrat (400,
401, 403, 404, 409, 422, 501, JSON invalide) pour produire des erreurs typées. Voir
`apps/llm_router/gateway/exceptions.py`.

## 6. Audit

Deux journaux **append-only**, sans prompt ni chunk ni secret :

* **`LlmEgressAuditEvent`** (`llm_egress_audit_event`) — un événement par appel :
  `requested_provider` **et** `effective_provider`, `gateway_attempted`, `policy_source`,
  `external_egress_allowed` (toujours faux), périmètre, `correlation_id`, tokens, latence,
  `prompt_chars` (une **longueur**, jamais un contenu).
* **`EgressPolicyChangeLog`** (`egress_policy_change_log`) — tout changement de
  `allow_external_fallback` : acteur, ancienne et nouvelle valeur, portée, horodatage.

L'audit est **fail-open** : une écriture qui échoue ne casse pas la génération, mais
laisse une ligne `llm_egress_audit_write_failed`.

## 7. Transparence utilisateur

Les réponses de chat (synchrone **et** SSE) portent `provider_effective` et `fallback_used`.
Ce sont des métadonnées de routage, jamais des données sensibles.

## 8. Exploitation

Posture par défaut, à conserver :

```bash
AI_GATEWAY_ENABLED=1
AI_GATEWAY_FALLBACK_ENABLED=0
AI_GATEWAY_FALLBACK_PROVIDER=none
CORE_CONFIG_RUNTIME_ENABLED=0
```

## 9. Limites connues

* La politique **par workspace** n'existe pas : la granularité s'arrête au RagSpace.
  L'overlay `CoreConfigOverride` reste **inerte au runtime** (`CORE_CONFIG_RUNTIME_ENABLED=0`).
* `Document.confidentiality` demeure un **champ inerte** : il ne protège rien
  aujourd'hui et ne doit pas être présenté comme un contrôle. Voir
  [../10-reference/limites-connues.md](../10-reference/limites-connues.md).
* `allow_external_fallback` n'est pas exposé dans l'interface et n'a aucun effet.
* Aucune notion de **région / résidence** des données.

## Références d'implémentation

- `backend/apps/llm_router/egress.py` (politique, contexte, exceptions)
- `backend/apps/llm_router/egress_audit.py` (émission des événements)
- `backend/apps/llm_router/router.py` (`_enforce_egress` — alias, `_generate_gateway_fallback`)
- `backend/apps/llm_router/embeddings.py` (barrière et audit du chemin embeddings)
- `backend/apps/llm_router/gateway/exceptions.py` (classification `fallback_eligible`)
- `backend/apps/audit/models.py` (`LlmEgressAuditEvent`, `EgressPolicyChangeLog`)
- `backend/api/v1/ragspaces/views.py` (audit du changement de politique)
- `tests/tests_root/llm_router/` (politique, matrice canari, audit)
- `tests/tests_root/llm_router/test_embedding_external_egress_guard.py` (garde embeddings)
