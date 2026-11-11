# Permissions et isolation

Objectif : décrire le modèle d'autorisation à trois couches de RagATM et ses garde-fous
(anti-énumération, corpus métier, authentification). Public : développeurs backend, auditeurs
sécurité.

L'isolation est **stratifiée**, chaque couche englobant la suivante :

```
propriétaire global (superuser) ⊃ RBAC workspace ⊃ RagSpace
```

> Extension planifiée (désactivée par défaut) : **cloisonnement par périmètre owner** à
> l'intérieur d'un même workspace — voir [owner-scope.md](owner-scope.md).

## Couche 1 — Propriétaire global RagATM

- Mappé sur `User.is_superuser` (helper `is_ragatm_owner` : authentifié + actif + `is_superuser`).
- Permission DRF **`IsRagATMOwner`**, appliquée à **tous** les endpoints d'administration corpus
  métier (`/api/v1/admin/…`) et de configuration Core (`/api/v1/admin/core-config/…`).
- **Orthogonal** aux rôles de workspace : un superuser administre le corpus partagé et la config,
  indépendamment de toute appartenance.

## Couche 2 — RBAC workspace

Rôles (`Membership.role`) : `owner`, `admin`, `member`, `viewer`.

| Capacité | owner | admin | member | viewer |
|---|:---:|:---:|:---:|:---:|
| Lire (documents, chat, RagSpaces) | ✅ | ✅ | ✅ | ✅ |
| Écrire (upload, ingest, chat mutatif) | ✅ | ✅ | ✅ | ❌ |
| Gérer membres / RagSpaces / réglages LLM | ✅ | ✅ | ❌ | ❌ |
| Consulter l'audit (rag-queries, llm-costs) | ✅ | ✅ | ❌ | ❌ |

Primitives d'accès (`apps/workspaces/services/access.py`) :

- `require_workspace_access(user, workspace_id, permission="read"|"write")` → renvoie le
  `Workspace` ou lève `PermissionDenied`. Le `viewer` est bloqué en écriture.
- `require_workspace_admin(...)` → exige un membership `owner`/`admin`.

Garde-fous : le dernier `owner` d'un workspace ne peut pas être retiré ; un `admin` ne peut pas
retirer un `owner`.

## Couche 3 — RagSpace

- `require_rag_space_access(user, rag_space_id, permission)` → renvoie le `RagSpace` après
  vérification de l'appartenance au workspace de l'espace ; **bloque les espaces soft-deleted** ;
  `viewer` bloqué en écriture.
- `rag_space` est **dupliqué sur `Chunk`** (et `DocumentSummary`) pour filtrer chaque requête de
  retrieval sans jointure coûteuse — garantie qu'aucun chunk d'un autre espace ne fuit.
- La façade Core (`/api/v1/core/`) délègue sans contrôle interne ; l'accès est **re-vérifié** par
  `require_rag_space_access` **avant** l'appel Core.

## Recherche et ouverture des conversations

La recherche `GET /api/v1/chat/threads/search/` (`apps/chat/services/thread_search.py`) applique
**deux conditions cumulatives**, recalculées à chaque requête, sans cache :

1. `ChatThread.user = request.user` : une conversation reste privée à son auteur ; un rôle
   d'administrateur ne donne accès à aucune conversation d'un autre membre ;
2. `ChatThread.rag_space_id ∈ searchable_rag_space_ids(user)` : espaces de
   `visible_ragspaces_for_user(mode="content")` (jamais `catalog`) sur les workspaces **actifs**.
   Un fil dont l'auteur a perdu l'accès à l'espace (membership retirée, ACL révoquée, périmètre
   owner changé, espace supprimé) n'apparaît plus.

Ce prédicat est verrouillé par un test d'équivalence dans les trois régimes (flags OFF,
owner-scope, ACL) : visible ⇒ `require_ragspace_access(read)` réussit, et réciproquement sur
les workspaces actifs. Les workspaces inactifs sont exclus de la recherche et du catalogue
multi-workspace (`GET /api/v1/rag-spaces/` sans `workspace_id`) sans que les règles d'accès au
**contenu** changent.

Seuls les messages `user`/`assistant` de statut vide ou `completed` sont cherchés ; jamais un
message système, en cours ou échoué, ni `sources`, `meta` ou le résumé de mémoire. Les extraits
ne viennent que de ces messages, sur les fils déjà filtrés. Le motif est construit côté serveur
(littéraux échappés + classes d'accents fixes, aucun opérateur saisi) et passé en paramètre SQL ;
`statement_timeout` local de 3 s (503 `search_timeout`). Exécution : un chemin rapide examine les
fils les plus récents par lots de 500 (messages lus fil par fil, par l'index `thread_id`), puis un
passage unique sur les messages des fils restants (union des motifs, agrégation `bool_or` par jeton) ;
même prédicat, même ordre. Banc hors ligne : p95 ≤ 282 ms à 63 Mo de messages (seuil du lot
d'index séparé : 75 Mo ou p95 > 300 ms mesuré).

L'ouverture d'un lien direct (`GET /api/v1/chat/threads/<uuid>/`) repasse par les mêmes gardes que
`/messages/` : fil de l'appelant (sinon 404), puis `require_ragspace_access(read)`.

## Anti-énumération

Pour ne pas révéler l'existence d'une ressource à un utilisateur non autorisé :

- Les primitives workspace renvoient le **même message** pour « introuvable » et « accès refusé ».
- L'accès document passe par `load_accessible_document` : **404** (et non 403) en cas d'absence
  de droit de **lecture** ; **403** uniquement sur un refus d'**écriture** (droit de lecture
  présent). Même logique 404 sur threads, analyses et corpus.

## Autorisation corpus métier

Le corpus métier partagé n'appartient à aucun workspace. Un workspace ne peut l'interroger que via
une ligne **`WorkspaceDomainCorpusAccess`** `enabled=True` (corpus `is_active`). L'administration
(création domaines/corpus, upload, réindexation, associations) est réservée au superuser
(`IsRagATMOwner`) ; un membre de workspace n'a qu'un accès **lecture seule** au listing des corpus
associés. Détails : [corpus-metier.md](corpus-metier.md).

## Authentification

Jeton DRF (`rest_framework.authtoken`) transporté par **cookie HttpOnly**
(`CookieTokenAuthentication`) :

1. Un header `Authorization: Token <clé>` est tenté d'abord (clients API/tests, **sans CSRF**) ;
2. sinon le jeton est lu depuis le cookie et la protection **CSRF** double-submit
   (cookie `csrftoken` + header `X-CSRFToken`) est **imposée** sur les méthodes non sûres.

Un cookie périmé/invalide **dégrade en anonyme** (renvoie `None`) plutôt que 401. Le login pose le
cookie d'auth + le cookie CSRF ; le logout supprime le jeton DRF + le cookie. **Pas de JWT, pas
d'auth de session API, pas d'inscription publique** (comptes créés par invitation /
réinitialisation de mot de passe).

Réglages DRF : `DEFAULT_AUTHENTICATION_CLASSES = [CookieTokenAuthentication]`,
`DEFAULT_PERMISSION_CLASSES = [IsAuthenticated]` ; `prod.py` ajoute le throttling.

Voir aussi la documentation d'exploitation (hors périmètre de cette édition),
[../05-api/authentification.md](../05-api/authentification.md) et le domaine
d'[invitations workspace](invitations.md) (rejoindre un workspace après acceptation ;
owner jamais attribuable par invitation).

## Références d'implémentation

- `backend/apps/accounts/authentication.py`, `backend/apps/accounts/permissions.py`
- `backend/apps/workspaces/services/access.py`
- `backend/apps/chat/services/thread_search.py`, `backend/api/v1/chat/views.py` (`ChatThreadSearchView`, `ChatThreadDetailView.get`)
- `tests/tests_root/http_api/test_chat_thread_search_api.py`
- `backend/api/v1/documents/` (`load_accessible_document`)
- `backend/apps/corpus/services/retrieval.py` (`allowed_corpus_ids_for_workspace`)
- `backend/config/settings/base.py` (bloc `REST_FRAMEWORK`), `backend/config/settings/prod.py`
