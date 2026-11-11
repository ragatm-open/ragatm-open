# API — Référence des endpoints

Objectif : référence consolidée de tous les endpoints de l'API v1, groupe par
groupe, avec méthode, chemin, permission et rôle. Public : intégrateurs et
développeurs.

Le CODE prévaut. Endpoints issus de `backend/api/v1/urls.py` et sous-modules.
Rappels : base `/api/v1/`, **slash final obligatoire**, auth par cookie token ou
`Authorization: Token` (voir [authentification.md](authentification.md)), codes
d'erreur détaillés dans [contrats-erreurs.md](contrats-erreurs.md).

Conventions de permission ci-dessous :

- **read** = membre du workspace ayant accès en lecture au RagSpace
  (`require_rag_space_access` read).
- **write** = idem en écriture (rôle `viewer` bloqué).
- **owner/admin** = membership `owner` ou `admin` (`require_workspace_admin`).
- **superuser** = propriétaire global RagATM (`IsRagATMOwner`, `is_superuser`).

---

## auth

Préfixe `/api/v1/auth/`. Détail et exemples dans
[authentification.md](authentification.md).

| Méthode | Chemin | Permission | Rôle |
|---|---|---|---|
| POST | `login/` | public | login → cookie token + CSRF |
| POST | `logout/` | authentifié | supprime token + cookie |
| GET | `me/` | authentifié | profil + `is_ragatm_owner` |
| POST | `password/reset/` | public | envoie le lien de reset (anti-énumération) |
| POST | `password/reset/confirm/` | public | pose le mot de passe / active le compte |

## bootstrap

Préfixe `/api/v1/bootstrap/`.

| Méthode | Chemin | Permission | Rôle |
|---|---|---|---|
| POST | `` | authentifié | idempotent — garantit workspace + RagSpace par défaut |

```bash
curl -X POST http://127.0.0.1:6811/api/v1/bootstrap/ \
  -H 'Authorization: Token <t>'
# → {"created":true,"workspace":{"id":1,"name":"Initial workspace","role":"owner"},
#    "ragspace":{"id":"<uuid>","name":"Default"}}
```

## capabilities

Préfixe `/api/v1/capabilities/`. Adossé à `apps.llm_router.capabilities`.

| Méthode | Chemin | Permission | Rôle |
|---|---|---|---|
| GET | `` | authentifié | providers/modèles chat autorisés + infos embeddings (aucun secret) |

Clé `ingest` — **source unique** de ce que le frontend propose au dépôt :

| Champ | Contenu |
|---|---|
| `ingest.formats` | clés de format réellement exploitables ici et maintenant (`pdf`, `docx`, `excel`, `pptx`, `csv`, `html`, `markdown`, `txt`, `image`, `msg`, `xml`, `dxf`, `ifc`, `dwg`). Un format sous flag n'y figure que si le flag est ouvert **et** la dépendance déployée |
| `ingest.image_formats` | sous-formats de la famille `image` (`jpeg`, `png`, `tiff`, `webp`…), dérivés de `DOCUMENT_IMAGE_FORMATS`. Vide si la porte image est fermée. `formats` annonce la FAMILLE, celui-ci dit LESQUELS |
| `ingest.eml` | état des courriels MIME `.eml` : `available` / `disabled_by_config`, `attachment_ingestion`, `space_scoped` |

Le frontend ne tient **aucune** allow-list de runtime : ouvrir un format côté serveur
suffit à le rendre déposable, sans modification du frontend
(voir [../04-frontend/frontend.md](../04-frontend/frontend.md)).

## workspaces

Préfixe `/api/v1/workspaces/`. Toutes authentifiées.

| Méthode | Chemin | Permission | Rôle |
|---|---|---|---|
| GET | `` | membre | liste des workspaces de l'utilisateur + rôle |
| GET | `<int:workspace_id>/members/` | owner/admin | liste des membres actifs (les invitations en attente n'y apparaissent pas) |
| POST | `<int:workspace_id>/members/` | owner/admin | **déprécié** : renvoie `409 use_invitations_endpoint` sous `WORKSPACE_INVITATIONS_V2_ENABLED` (défaut). Inviter via `invitations/`. Owner jamais attribuable. |
| PATCH | `<int:workspace_id>/members/<int:membership_id>/` | owner/admin | change de rôle (owner exclu ; protège le dernier owner ; un admin ne peut retirer un owner) |
| DELETE | `<int:workspace_id>/members/<int:membership_id>/` | owner/admin | retire un membre (protège le dernier owner) |
| POST | `<int:workspace_id>/invitations/` | owner/admin | crée une invitation, renvoie le lien one-shot `invitation_url` (`no-store`). Rôle ∈ {admin, member, viewer} |
| GET | `<int:workspace_id>/invitations/` | owner/admin | liste des invitations (statut/rôle/génération) — **jamais** de token ni d'URL |
| POST | `<int:workspace_id>/invitations/<uuid:invitation_id>/regenerate/` | owner/admin | régénère le token (ancien invalidé), `generation+1`, nouveau lien |
| POST | `<int:workspace_id>/invitations/<uuid:invitation_id>/revoke/` | owner/admin | révoque (idempotent ; refuse si acceptée) |
| GET | `<int:workspace_id>/ragspaces/` | owner/admin | liste des RagSpaces non supprimés |
| POST | `<int:workspace_id>/ragspaces/` | owner/admin | crée un RagSpace (valide providers/modèles via `capabilities`) |
| GET | `<int:workspace_id>/domain-corpora/` | membre (read) | corpus métier associés et actifs (lecture seule) |

```bash
curl http://127.0.0.1:6811/api/v1/workspaces/ -H 'Authorization: Token <t>'
```

Domaine détaillé : [../03-backend/invitations.md](../03-backend/invitations.md).

## invitations (acceptation publique)

Préfixe `/api/v1/invitations/`. Le **token** est la crédence : il transite dans le corps
(jamais dans l'URL de ces appels). Réponses `no-store` + `no-referrer`.

| Méthode | Chemin | Permission | Rôle |
|---|---|---|---|
| POST | `validate/` | public | état du token (`pending`/`expired`/`accepted`/`revoked`) + `account_active` |
| POST | `accept/` | public | compte nouveau/inactif : pose le mot de passe, crée le membership (rôle = invitation) |
| POST | `accept-authenticated/` | authentifié | compte actif : crée le membership, mot de passe **jamais** modifié (email de session == invitation) |

## rag-spaces

Préfixe `/api/v1/rag-spaces/`. Toutes authentifiées. `<uuid>` = id de RagSpace.

| Méthode | Chemin | Permission | Rôle |
|---|---|---|---|
| GET | `` | membre | liste des RagSpaces accessibles (`?workspace_id=` optionnel ; **sans** `workspace_id`, workspaces **actifs** seulement) ; annote `reindex_required` |
| PATCH | `<uuid>/` | owner/admin | modifie `name`/`llm_provider`/`llm_model`/`embedding_*`/`allow_external_fallback` (un changement d'embedding marque tous les docs `reindex_required` + journalise `LlmSettingsChangeLog`) |
| DELETE | `<uuid>/` | owner/admin | **purge irréversible** : RagSpace + documents + chunks + **fichiers** (204). 404 si inexistant/déjà purgé ; 500 = rien supprimé. Voir [../03-backend/suppression-ragspace.md](../03-backend/suppression-ragspace.md) |
| GET | `<uuid>/stats/` | read | stats documents/chunks + couverture (SQL seul, sans LLM) |
| GET | `<uuid>/llm-settings/` | owner/admin | réglages chat + embedding courants + capacités provider |
| POST | `<uuid>/llm-settings/test/` | owner/admin | sonde de connexion au provider |
| POST | `<uuid>/reindex/` | owner/admin | crée un **job de réindexation durable** et rend la main aussitôt (jamais de traitement inline) : **202** `{status:accepted, job_id, total_documents}` ; **409** si un job est déjà actif (renvoie l'existant) ; **503** au-delà de la garde EAGER. Voir [../03-backend/reindexation-jobs.md](../03-backend/reindexation-jobs.md) |
| POST | `<uuid>/retry-failed-documents/` | owner/admin | **reprise en masse des documents en erreur** (sélection `explicit` par ids, ou `filter` = tous les résultats du filtre moins exclusions) + `strategy` (`auto` par défaut, `standard`, `force_ocr`, `extended_timeout`). Même job durable, `kind=failed_documents_retry`. **202** `{job_id, kind, requested_strategy, strategy_breakdown, skip_reasons, total_documents, skipped_documents}` ; **409** `already_running` / `no_eligible_documents` / `no_retryable_documents` / `selection_too_large` ; **400** `invalid_selection`. Ne crée aucun document et ne consomme aucun quota |
| POST | `<uuid>/retry-failed-documents/preview/` | owner/admin | **prévisualisation** de la campagne : mêmes permissions et même sélection, mais **ne crée rien** et ne lit aucun fichier. **200** `{strategy_breakdown, skip_reasons, actionable_documents, skipped_documents, extended_timeout_seconds}` |
| POST | `<uuid>/analyses/` | read | crée un job d'analyse globale de corpus (202 créé / 200 réutilisé / 400/409 sur limites) |

```bash
curl "http://127.0.0.1:6811/api/v1/rag-spaces/<uuid>/stats/" \
  -H 'Authorization: Token <t>'
```

Le contrat détaillé de `stats/` (blocs `content`/`ratios`/`scope`/`coverage`,
niveaux A/B) est décrit dans
[coverage-semantics](../10-reference/coverage-semantics.md).

## documents

Préfixe `/api/v1/documents/`. Toutes authentifiées. Isolation via
`load_accessible_document` (404 anti-énumération sans accès lecture, 403 sur refus
d'écriture). `<uuid>` = id de document.

| Méthode | Chemin | Permission | Rôle |
|---|---|---|---|
| GET | `` | read | liste paginée du RagSpace (`?rag_space_id=&limit=&offset=&status=&search=&ordering=`). Défaut `limit=50`, **maximum 200**. Renvoie `total`, `has_next`/`has_previous`, `ordering` et `counts` (compteurs par statut sur **tout** l'espace, calculés après recherche et avant filtre). `status` ou `ordering` hors allowlist ⇒ **400**. Voir [../03-backend/liste-documents.md](../03-backend/liste-documents.md) |
| GET | `search/` | read | documents mentionnant un terme (`?rag_space_id=&q=&fts=&lang=`) |
| GET | `<uuid>/` | read | détail (+ vue d'extraction sûre) |
| POST | `ingest/` | write | ingestion texte `{rag_space_id, text, filename?, async?}` |
| POST | `upload/` | write | upload multipart `file=` ; validation taille/pages/format avant création ; Excel/PPTX/OCR forcent l'async ; rejets 413/415/400 |
| POST | `<uuid>/reindex/` | write | réindexe depuis `storage_file`. Corps `{"strategy": "auto\|standard\|force_ocr\|extended_timeout"}` ; OCR et délai étendu forcent l'async. **400** `invalid_strategy`, `strategy_not_supported` (OCR sur un format qui n'en relève pas), `document_not_eligible` (échec définitif). `?ocr=true` **déprécié**, traduit en `force_ocr`. Voir [../03-backend/reindexation-jobs.md](../03-backend/reindexation-jobs.md) **409** `source_file_missing` (source déclarée mais introuvable dans le stockage) ou `no_storage_file_for_reindex` (aucune source conservée) — la réponse porte `error_code`, `message` et `retryability: replace_source_recommended` : le geste utile est de fournir la source, pas de réessayer. **409** `reindex_already_in_progress` si le document est déjà `PENDING`/`INGESTING` (admission atomique sous verrou, même ou autre stratégie ; corps `{ok:false, error_code, document_id, status, force_allowed:true, active_job_id?, active_item_id?}`) — corps `{"force": true}` (booléen JSON strict, sinon **400** `invalid_force`) pour relancer malgré tout ; `force` ne contourne ni l'accès ni l'éligibilité. |
| DELETE | `<uuid>/delete/` | write | hard delete (doc + chunks + résumés + fichier) ; 409 si `PENDING`/`INGESTING`. La réponse distingue `file_deleted` (le fichier était là et a été supprimé) de `file_already_missing` (il n'y était plus, rien n'a été fait) — jamais confondus. |
| GET | `<uuid>/download/` | read | sert l'original avec en-têtes de sécurité de téléchargement |
| GET | `<uuid>/tabular-preview/` | read | aperçu **borné** d'un classeur `.xlsx` : fenêtre de cellules autour de la plage citée, surlignage, inventaire des feuilles indexées. Paramètres `chunk_id`, `sheet_index` (**1-based**), `sheet_name`, `range`, `context_rows`, `context_columns`, `sha256`. Refus typés (`chunk_stale`, `sheet_not_previewable`…). Voir [../03-backend/apercu-tabulaire.md](../03-backend/apercu-tabulaire.md) |
| GET | `<uuid>/presentation-preview/` | read | aperçu **borné** d'un `.pptx` : fenêtre de diapositives autour de la source, surlignage **au caractère**, inventaire, notes séparées. Paramètres `chunk_id`, `slide_number` (**1-based**), `context_before`, `context_after`, `sha256`. Gardes de version et de configuration des notes. Flag `DOCUMENT_PPTX_PREVIEW_ENABLED`. Voir [../03-backend/apercu-presentation.md](../03-backend/apercu-presentation.md) |
| GET / POST | `<uuid>/summary/` | read (viewers admis) | GET = état en cache ; POST = génère/réutilise (202 si doc long) ; `thread_id` + `request_id` ⇒ persistance dans le fil. **Documents privés uniquement** — un document de corpus métier passe par [`domain-corpus-documents/`](#domain-corpus-documents) |

**Résumé rattaché au fil de discussion** (*implémenté*). `POST <uuid>/summary/` accepte deux
champs **facultatifs mais indissociables** :

| Champ | Effet |
|---|---|
| `thread_id` | fil où persister le résumé. **Validé** : doit appartenir à l'appelant et au même RagSpace que le document, sinon `404` indistinguable d'un fil inexistant |
| `request_id` | clé d'idempotence. **Obligatoire** dès que `thread_id` est fourni (`400 request_id_required`) — sans elle, un retry réseau créerait un second résumé |

Fournis, un message assistant `pending` est créé **à la date de la demande** (position
chronologique correcte même si la génération est asynchrone), puis passé à `completed`/`failed`
sans jamais être remplacé par un second message. La réponse porte alors `message_id` et
`request_id` pour la réconciliation frontend. Omis, le comportement historique est **inchangé** :
aucune écriture dans un fil (cas de la modale Documents).

Relances : `request_id` identique + message `completed` ⇒ rejeu strict sans nouvelle génération ;
message `failed` ⇒ le **même** message est ré-ouvert et régénéré. Dans les deux cas, jamais de
doublon.

```bash
curl -X POST "http://127.0.0.1:6811/api/v1/documents/upload/?rag_space_id=<uuid>&async=true" \
  -H 'Authorization: Token <t>' \
  -F 'file=@/chemin/doc.pdf'
# → {"ok":true,"queued":true,"document_id":"<uuid>","deduped":false}
```

Formats acceptés : TXT, Markdown, PDF, DOCX, CSV, HTML, images, XLSX. Voir
[ingestion-extraction](../03-backend/ingestion-extraction.md).

**Audio** (*activable par flag*) : `POST /api/v1/documents/upload-audio/?rag_space_id=<uuid>`
(même formulaire multipart). Chemin RÉSERVÉ à l'audio (`.mp3 .m4a .wav .flac .ogg .webm`,
≤ `DOCUMENT_AUDIO_MAX_UPLOAD_BYTES` = 600 000 000 octets) ; tout autre format → `415 unsupported_audio`.
Refus : `413 audio_file_too_large` (`max_bytes`), `400 audio_ingestion_disabled` (drapeau fermé, ou espace
non listé hors mode général `DOCUMENT_AUDIO_GLOBAL_ENABLED`), `409` quota. Réponse : `{"ok":true,"queued":true,"document_id":…}` — la transcription est
asynchrone ; le document reste `PENDING` (sous-étape `audio.stage`) jusqu'à `READY`/`FAILED`.
Un document sérialisé porte alors `audio` = `{format, stage, error_code, language, duration_ms,
parts_total, parts_done, segment_count}` (jamais de texte ni d'identifiant de job). La suppression
d'un audio PENDING en cours de transcription est permise (jobs Gateway libérés).
`GET /api/v1/capabilities/` expose `ingest.audio` (`status`, `extensions`, `max_upload_bytes`,
`max_duration_seconds`, `upload_path`, `space_scoped` — faux en mode général) ; chaque RagSpace listé
porte `audio_upload_enabled`.
Voir [ingestion-audio](../03-backend/ingestion-audio.md).

## domain-corpus-documents

Préfixe `/api/v1/domain-corpus-documents/`. Toutes authentifiées. Isolation via
`load_accessible_domain_corpus_document` (association `WorkspaceDomainCorpusAccess`
active + corpus actif) ; `404 not_found` **anti-énumération** pour toute cause
d'inaccessibilité. `<uuid>` = id de `DomainCorpusDocument` — espace de noms **disjoint**
de celui de `documents/`. *Activé par défaut* (`DOMAIN_CORPUS_SUMMARY_ENABLED`, défaut `1` ;
à `0` l'endpoint répond `404`).

| Méthode | Chemin | Permission | Rôle |
|---|---|---|---|
| GET | `<uuid>/summary/` | lecture du corpus par le workspace (viewers admis) | état en cache (`status: none` si absent) |
| POST | `<uuid>/summary/` | idem | génère/réutilise le résumé (202 si document long) |

Corps du POST : `{mode, scope, page, language, refresh, rag_space_id, thread_id, request_id}`.
`rag_space_id` désigne l'espace **consommateur** — il porte la politique LLM/egress, le corpus
n'en ayant pas ; il est **confronté** à `require_ragspace_access` et n'est jamais cru sur
parole. Le `workspace` en est dérivé, jamais accepté du client. Fourni avec `thread_id`, les
deux doivent désigner le même espace (`404` sinon).

Erreurs : `404 not_found` (inexistant / corpus inactif / non associé / association désactivée /
flag off), `409 document_not_ready`, `409 document_no_content` (document ou **page** sans
extrait), `409 request_id_conflict` (`request_id` déjà employé pour une autre cible),
`400 request_id_required`, `502 summary_provider_unavailable`.

Voir [../03-backend/resume-corpus-metier.md](../03-backend/resume-corpus-metier.md).

## chat

Préfixe `/api/v1/chat/` (+ alias `feedback/`). Toutes authentifiées.

> **`question` est bornée à 4 000 caractères** (`MAX_QUESTION_CHARS`,
> [`api/v1/limits.py`](../../backend/api/v1/limits.py)). Au-delà : **400**, jamais de
> troncature silencieuse — une question coupée produirait une réponse à une autre
> question. La même borne s'applique à `POST core/retrieve/`, qui emprunte le même
> classifieur d'intention.
>
> **Pourquoi** : ce classifieur s'exécute sur chaque requête *avant tout retrieval*, et
> son coût est quadratique sur certaines entrées adversariales — 4 000 caractères de
> guillemets coûtent 242 ms, 16 000 en coûtent 3 910. Sans borne, une requête
> authentifiée de 100 Ko occupait un worker plusieurs minutes. **Valeur choisie sur les
> usages mesurés** (production : max 164 caractères ; staging : 324 ; jeux de
> qualification : 413), soit ~10× la plus longue entrée légitime observée.

| Méthode | Chemin | Permission | Rôle |
|---|---|---|---|
| POST | `chat/` | read | réponse RAG synchrone `{rag_space_id, question, thread_id?, corpus_choice?}` |
| POST | `chat/stream/` | read | même contrat, réponse **SSE** (`text/event-stream`) |
| GET | `chat/threads/` | read | liste des threads (`?rag_space_id=`) : les 100 plus récents ; `&before=<uuid d'un fil de l'appelant dans cet espace>` donne les 100 suivants, strictement plus anciens dans l'ordre `updated_at DESC, id DESC` (400 `invalid_parameter`, 404 `not_found` si le fil d'ancrage n'est pas le sien ou pas dans l'espace) |
| GET | `chat/threads/search/` | authentifié | recherche des **propres** conversations de l'appelant sur ses espaces lisibles des workspaces actifs (`?q=` 2–200 car., `scope=all\|title`, `rag_space_id`, `workspace_id`, `limit` 1–50 déf. 20, `cursor`). Réponse `{results:[{id,title,rag_space_id,rag_space_name,workspace_id,workspace_name,updated_at,match,snippet}], next}`, **jamais de total** ; filtre hors périmètre ⇒ 200 `[]`. 400 `q_too_short`/`q_too_long`/`invalid_cursor`/`invalid_parameter`, 429 (scope dédié `chat_thread_search`), 503 `search_timeout`. Voir [../03-backend/permissions-isolation.md](../03-backend/permissions-isolation.md) |
| GET | `chat/threads/<uuid>/` | read | résout un lien direct : `{id,title,rag_space_id,workspace_id,created_at,updated_at}` ; 404 si le fil n'est pas à l'appelant, garde de lecture de l'espace ensuite |
| POST | `chat/threads/` | read | crée un thread `{rag_space_id, title?}` |
| GET | `chat/threads/<uuid>/messages/` | read | messages (+ `awaiting_corpus_choice` calculé serveur ; `kind`/`status`/`document_id`/`summary_id` pour les résumés) |
| PATCH | `chat/threads/<uuid>/` | read | renomme le thread `{title}` |
| GET | `chat/threads/<uuid>/rephrasing-suggestions/` | read | statut asynchrone des suggestions (`?request_id=`) |
| GET | `chat/threads/<uuid>/analyses/` | read | analyses lancées depuis ce thread |
| POST | `feedback/` et `chat/feedback/` | authentifié | `{log_id, score(1|-1), kind?, comment?, expected_document?, expected_term?}` sur ses propres logs. Crée un **`AnswerFeedback` durable** ; `409 feedback_exists` si un retour existe déjà. Débit `10/min` par utilisateur. Voir [feedback-incidents](../03-backend/feedback-incidents.md) |
| PATCH | `chat/feedback/<uuid>/` | propriétaire | corrige son propre retour `{kind?, comment?, expected_document?, expected_term?}` — **jamais la note** |
| GET | `chat/feedback/<uuid>/` | propriétaire | accusé de réception (aucune donnée sensible) |

Avant tout retrieval, la vue applique des intercepteurs déterministes (indépendants
des flags) : `/help`, choix de corpus-mode (L3), garde analytique Excel (E4).

### Événements SSE de `chat/stream/`

Séquence : `meta` → `sources` → `token` (un par token) → `done`, ou `error`.

- `meta` : `{request_id, log_id, rag_space_id, thread_id, answer_kind}`
- `sources` : `{sources: [...]}` (contrat sources, voir [contrats-erreurs.md](contrats-erreurs.md#contrat-des-sources))
- `token` : `{delta: "..."}`
- `done` : `{answer, sources, provider, model, tokens, usage, thread_id, status, log_id, answer_kind}`
- `error` : `{detail, request_id, thread_id?}`

Le flux se ferme immédiatement après `done`. Quand l'AI Gateway est actif, le
streaming réel de tokens est remplacé par un unique bloc pseudo-stream (voir
[AI Gateway](../06-ai/ai-gateway.md)). `answer_kind` ∈
`ok | explain | partial | low_confidence | news | no_answer | fallback_general | numeric_evidence_unverified`.
`done` (et la réponse synchrone) portent aussi `numeric_evidence_guard` :
`{verdict: pass|redact|block, claims, supported, unverified, unsupported, conflicting,
propositions_removed, propositions_reduced, attributions_removed, citations_dropped, absences_removed,
units_removed, quantifications_removed, list_attributions_removed, documents_consulted}` (ou `null` hors périmètre si le contrôle a échoué) ; `units_removed` =
unités affichées retirées parce que la source qui porte la valeur ne les établit pas (valeur conservée) ;
`quantifications_removed` = phrases retirées affirmant qu'une source ne chiffre pas un élément alors que le
passage cité de cette source porte le chiffre (contrôle actif aussi hors périmètre ; le flux hors périmètre
n'est pas tamponné : la correction arrive avec `done`) ;
`list_attributions_removed` = phrases retirées qui rattachaient une valeur d'une énumération « respectively » de la
source citée à une autre entité de l'énumération ;
`numeric_evidence_unverified` = aucun nombre de la réponse n'est étayé par une source vérifiable,
réponse remplacée par une abstention contrôlée. Dans le périmètre du contrôle (contexte avec OCR ou
lecture visuelle), le flux n'émet qu'**un** événement `token` (la réponse validée) avant `done` (voir
[pipeline-retrieval.md §10](../03-backend/pipeline-retrieval.md)). Chaque source porte
`meta.numeric_evidence` / `meta.numeric_evidence_reason`.

```bash
curl -X POST http://127.0.0.1:6811/api/v1/chat/ \
  -H 'Authorization: Token <t>' -H 'Content-Type: application/json' \
  -d '{"rag_space_id":"<uuid>","question":"Quelle est la politique de congés ?"}'
```

## analyses

Préfixe `/api/v1/analyses/`. Toutes authentifiées ; accès revérifié à chaque
opération (404 anti-énumération). `<uuid>` = id de job.

| Méthode | Chemin | Permission | Rôle |
|---|---|---|---|
| GET | `<uuid>/` | read | détail du job d'analyse |
| POST | `<uuid>/cancel/` | demandeur OU owner/admin OU superuser | annulation coopérative |

La création d'un job passe par `POST /rag-spaces/<uuid>/analyses/` (voir
[rag-spaces](#rag-spaces)).

## prompts

Préfixe `/api/v1/prompts/`. Toutes authentifiées. `<uuid>` = id de template.

| Méthode | Chemin | Permission | Rôle |
|---|---|---|---|
| GET | `` | membre | liste (`?workspace_id=&name=&active_only=`) |
| POST | `` | membre non-viewer | crée une version `{workspace_id, name, content, activate?}` (viewer → 403) |
| GET | `<uuid>/` | membre | détail |
| POST | `<uuid>/activate/` | membre non-viewer | active cette version (viewer → 403) |

## audit

Préfixe `/api/v1/audit/`. Toutes authentifiées ; contrôle owner/admin en vue.
`<uuid>` = id de log.

| Méthode | Chemin | Permission | Rôle |
|---|---|---|---|
| GET | `rag-queries/` | owner/admin du workspace | logs de requêtes RAG (`?workspace_id=&rag_space_id=&limit=`) |
| GET | `rag-queries/<uuid>/` | owner/admin | détail d'un log |
| GET | `llm-costs/` | owner/admin | coûts/tokens agrégés (`?workspace_id=&days=&group_by=&period=`) |

`llm-costs/` s'appuie sur `RagQueryLog.llm_cost_estimated` et
`apps/audit/aggregations.py`. Le coût est `0.0` pour les modèles locaux (ollama),
`None` pour un modèle inconnu. Voir [gouvernance de l'audit](../03-backend/taches-asynchrones.md).

## core (façade RagATM_Core)

Préfixe `/api/v1/core/`. Toutes authentifiées ; accès revérifié via
`require_rag_space_access` avant l'appel Core.

| Méthode | Chemin | Permission | Rôle |
|---|---|---|---|
| GET | `health/` | authentifié | santé du moteur (sans secrets) |
| POST | `retrieve/` | read | retrouve les passages **sans génération** `{rag_space_id, question}` |
| GET | `documents/<uuid:document_id>/status/` | read | statut d'ingestion (`?rag_space_id=`) |

`retrieve/` expose la MÊME façade que celle qu'emprunte le chat (`core.retrieve`,
défaut). Voir [pipeline retrieval](../03-backend/pipeline-retrieval.md).

## admin/core-config (superuser)

Préfixe `/api/v1/admin/core-config/`. Toutes `[IsAuthenticated, IsRagATMOwner]`.
Les vues restreignent `http_method_names` aux verbes exacts.

| Méthode | Chemin | Permission | Rôle |
|---|---|---|---|
| GET | `effective/` | superuser | config effective (env + overlay DB optionnel), rédigée |
| GET | `health/` | superuser | santé Core/Gateway : `core_health.ok = null` (non vérifié — jamais « ok » sans preuve) et `gateway_health` réel (`status` AVAILABLE/DEGRADED/UNAVAILABLE/UNKNOWN par service, sondes `GET /health/*` en cache 30 s) |
| GET | `document-capabilities/` | superuser | **capacités documentaires effectives** de l'environnement (lecture seule) : formats, capacités, matrice, services AI Gateway, fournisseurs d'embeddings par espace, cohérence des workers, alertes. `?ragspace_id=<uuid>` (vue consultative d'un espace : 400 si invalide, 404 si inconnu), `?refresh=1` (relance des sondes, au plus une fois par 10 s et par utilisateur). `Cache-Control: no-store`. Voir [capacités documentaires](../03-backend/capacites-documentaires.md) |
| GET | `audit/` | superuser | historique `LlmSettingsChangeLog` |
| GET | `config-audit/` | superuser | historique `CoreConfigAuditEvent` (rédigé) |
| PUT | `override/` | superuser | pose un override — **403 en prod**, **409** sauf `CORE_CONFIG_EDIT_ENABLED` (staging) |
| POST | `override/unset/` | superuser | retire un override — mêmes gardes |

L'overlay n'a d'effet runtime que si `CORE_CONFIG_RUNTIME_ENABLED`. Voir
[configuration dynamique](../03-backend/configuration-dynamique.md).

## admin/ (corpus métier, superuser)

Préfixe `/api/v1/admin/`. Toutes `[IsAuthenticated, IsRagATMOwner]`. Ingestion de
corpus **synchrone** (pas de Celery), Excel refusé. `<uuid>` selon le segment.

| Méthode | Chemin | Rôle |
|---|---|---|
| GET / POST | `business-domains/` | liste / crée un domaine métier |
| GET / PATCH / DELETE | `business-domains/<uuid:domain_id>/` | détail / modifie / supprime (cascade) |
| GET / POST | `domain-corpora/` | liste / crée un corpus |
| GET / PATCH / DELETE | `domain-corpora/<uuid:corpus_id>/` | détail / modifie / supprime |
| GET / POST | `domain-corpora/<uuid:corpus_id>/documents/` | liste / upload (ingest sync, `allow_excel=False`, `allow_pptx=False`) |
| POST | `domain-corpora/<uuid:corpus_id>/reindex/` | réindexe tout le corpus |
| GET / DELETE | `domain-corpus-documents/<uuid:document_id>/` | détail / supprime un document de corpus |
| POST | `domain-corpus-documents/<uuid:document_id>/reindex/` | réindexe un document de corpus |
| GET / POST | `workspaces/<int:workspace_id>/domain-corpora/` | liste / associe un corpus à un workspace |
| DELETE | `workspaces/<int:workspace_id>/domain-corpora/<uuid:corpus_id>/` | dissocie (idempotent) |

L'accès d'un workspace à un corpus repose **uniquement** sur une association
`WorkspaceDomainCorpusAccess` active. Voir
[corpus métier](../03-backend/corpus-metier.md).

## Voir aussi

- [Vue d'ensemble de l'API](vue-ensemble.md)
- [Authentification](authentification.md)
- [Contrats d'erreurs](contrats-erreurs.md)
- [Permissions et isolation](../03-backend/permissions-isolation.md)

## Références d'implémentation

- `backend/api/v1/urls.py` et sous-modules (`auth/`, `bootstrap/`, `workspaces/`,
  `ragspaces/`, `documents/`, `chat/`, `analyses/`, `prompts/`, `audit/`, `core/`,
  `admin_core_config/`, `admin_corpus/`, `capabilities`)
- `backend/apps/workspaces/services/access.py`
- `backend/apps/accounts/permissions.py`
- `backend/apps/rag_core/services/coverage_service.py`
- `backend/apps/corpus/services/`
- `backend/apps/audit/aggregations.py`

### Documents — `visual_enrichment_status` *(facultatif)*

Présent sur un document dont l'enrichissement visuel est DIFFÉRÉ (VISUAL-ENRICHMENT-DEFERRED) :
`pending` | `running` | `partial` | `succeeded` | `failed` | `not_applicable`. `status=READY`
signifie « exploitable par la recherche », **pas** « vision terminée » ; aucun client n'est
tenu de lire ce champ.
