# Frontend (Next.js)

Cette page documente l'application web RagATM (répertoire `frontend/`). Public visé : développeurs front et intégrateurs. Le **code est la source de vérité** ; les faits ci-dessous sont vérifiés contre le dépôt.

## 1. Stack technique

| Élément | Valeur | Note |
|---|---|---|
| Framework | **Next.js 15.5.29** | App Router (aucun répertoire `pages/`) ; APIs dynamiques asynchrones (`await cookies()`, `await headers()`, `await params`) |
| UI | **React 19.2.9** + `react-dom` 19.2.9 | `reactStrictMode: true` |
| Langage | **TypeScript 5.9.3** | `tsconfig.json` en `"strict": false`, `moduleResolution: "bundler"`, alias `@/* → src/*` |
| Gestionnaire de paquets | **npm** | seul `package-lock.json` présent (pas de pnpm/yarn) |
| Dépendances runtime tierces | **`react-markdown` ^10.1.0** + **`remark-gfm` ^4.0.1** | ce sont les **deux seules** deps runtime hors React ; l'ancienne doc « aucune dépendance UI tierce » est donc **inexacte** |
| Police | `next/font/google` Hanken Grotesk | chargée dans `src/app/layout.tsx` |
| Types | `@types/react` **19.2.7**, `@types/node` ^25.0.3 | alignés sur React 19 depuis RC2 |

Statut : **implémenté**. Le design system est maison (voir §8), il n'y a pas de bibliothèque de composants tierce (MUI, Chakra, etc.) — mais dire « aucune dépendance UI » est faux à cause de `react-markdown`/`remark-gfm`.

### 1.1 Scripts npm (`frontend/package.json`)

| Script | Commande | Rôle |
|---|---|---|
| `dev` | `next dev` | serveur de dev (lancé en loopback `-H 127.0.0.1 -p 6300`, cf. politique ports) |
| `build` | `next build` | build de production |
| `start` | `next start` | serveur de production |
| `lint` | `next lint` | ESLint (config Next) |
| `test` | `vitest run` | tests unitaires + composants (hook `pretest` : `scripts/link-test-deps.mjs`) |
| `test:watch` | `link-test-deps.mjs` puis `vitest` | mode watch |

### 1.2 `next.config.mjs`

- `reactStrictMode: true`.
- `trailingSlash: false` + `skipTrailingSlashRedirect: true` : Next ne doit ni ajouter ni retirer de slash final — c'est la couche API qui gère les slashes elle-même (voir §4).
- `headers()` pose une **Content-Security-Policy** autorisant `blob:`/`https:` (embarquement PDF via iframe/PDF.js) et scripts/styles inline. Prévue pour coexister avec la CSP nginx en production (la documentation d'exploitation (hors périmètre de cette édition)).

## 2. Table des routes (App Router)

Toute l'UI vit sous `src/app`. Le layout racine `src/app/layout.tsx` (`<html lang="fr">`) charge `tokens.css` et la police. Chaque écran authentifié se compose via `<AppShell>` (voir §7).

| Route | Fichier | Rôle | Accès |
|---|---|---|---|
| `/` | `src/app/page.tsx` | Portail de redirection : appelle `GET /api/v1/auth/me/` → `/login` **sur 401 seulement**, `/home` sinon (un 429/5xx/réseau est géré par la coquille, cf. §5.1) | public |
| `/login` | `src/app/login/page.tsx` | Connexion en deux facteurs : email/mot de passe → `POST /api/v1/auth/login` → écran **code TOTP** (`mfa/verify`) ou **premier enrôlement** (`mfa/setup` : QR calculé dans le navigateur + clé manuelle, puis `mfa/setup/confirm`) ; `bootstrap()` et redirection `/chat` **seulement après** le second facteur. Au montage, `GET mfa/status` reprend l'étape après un rafraîchissement. Aucune donnée MFA en `localStorage`/`sessionStorage`. Lien vers reset. Colonne droite : scène illustrative du pipeline RAG (`components/login/PremiumRagPipelineHero`, cf. §« Page de connexion — scène du pipeline ») | public |
| `/reset-password` | `src/app/reset-password/page.tsx` + `ResetPasswordClient.tsx` | Confirmation de réinitialisation via `uid`/`token` en query. Page serveur enveloppant le client dans `<Suspense>` (requis par `useSearchParams`) | public |
| `/home` | `src/app/home` (`page.tsx`) | Tableau de bord post-login : accueil, liste des RagSpaces en cartes, CTA vers `/chat` | connecté |
| `/chat` | `src/app/chat/page.tsx` | **Écran principal** : assistant RAG. Chat SSE en streaming, panneau de sources, choix de portée corpus, résumés de documents, analyses de corpus, suggestions de reformulation | connecté |
| `/documents` | `src/app/documents/page.tsx` | Gestion documentaire du RagSpace courant : upload (unitaire + import de dossier), liste **paginée côté serveur** (recherche, filtres de statut et tri exécutés par l'API — cf. [../03-backend/liste-documents.md](../03-backend/liste-documents.md)), badges de statut, suppression, résumé par document, ouverture du visualiseur | connecté |
| `/doc/[documentId]` | `src/app/doc/[documentId]/page.tsx` | Visualiseur de document — **routeur par type** (`preview_kind`) : PDF en blob avec deep-link `#page=`, classeur en aperçu tabulaire HTML, état explicite sinon. Voir [visualiseur-documents.md](visualiseur-documents.md). Route dynamique | connecté |
| `/corpora` | `src/app/corpora/page.tsx` | Liste **en lecture seule** des corpus métier associés au workspace (`listWorkspaceCorpora`) | connecté |
| `/workspace-admin` | `src/app/workspace-admin/page.tsx` | Administration du workspace (**adminOnly**). Onglets `members` / `spaces` / `audit` : membres, RagSpaces (onglet `spaces` dans `_components/SpacesTab.tsx`, créer/éditer via `SpaceEditModal`), audit `RagQueryLog` | admin |
| `/ragatm-admin` | `src/app/ragatm-admin/page.tsx` | **Propriétaire global uniquement**. Domaines métier + corpus : créer domaines/corpus, upload/reindex/suppression de docs corpus, affectation aux workspaces (`src/lib/corpus.ts`) | superuser |
| `/admin-core` | `src/app/admin-core/page.tsx` | **Propriétaire global uniquement**. Administration technique : config effective, santé (santé Gateway réelle ; « Inconnu » sans preuve), audit cœur, lien vers les capacités documentaires (`src/lib/coreConfig.ts`, vue `_components/AdminCoreView.tsx`) | superuser |
| `/admin-core/document-capabilities` | `src/app/admin-core/document-capabilities/page.tsx` | **Propriétaire global uniquement**, **lecture seule**. « Capacités documentaires » : environnement et version lus du backend, matrice format × traitement, détail par format, images intégrées, services AI Gateway, embeddings par espace, cohérence des workers, filtre consultatif par RagSpace ; seul bouton : « Actualiser ». Voir [capacités documentaires](../03-backend/capacites-documentaires.md) | superuser |
| `/api/[...path]` | `src/app/api/[...path]/route.ts` | **Pas une page** — reverse-proxy serveur vers Django (voir §3) | — |

> Correction vs ancienne doc : les pages `/home`, `/documents`, `/corpora`, `/ragatm-admin`, `/admin-core` **existent** et manquaient à l'ancien inventaire.

Le code propre à une route utilise la convention des dossiers `_components/` et `_lib/` (préfixe `_`, exclus du routage). Le chat a le `_lib` le plus riche : `sse.ts`, `chatRequest.ts`, `chatMessages.ts`, `chatMessageState.ts`, `analysisPoll.ts`, `rephrasingPoll.ts`, `sources.ts`, `storage.ts`, `suggestions.ts`, `summaryIntent.ts`, `types.ts`.

## 3. Architecture reverse-proxy (fait structurant)

`src/app/api/[...path]/route.ts` est un **Route Handler catch-all** (GET/POST/PUT/PATCH/DELETE/OPTIONS) qui relaie chaque appel navigateur `/api/*` vers le backend Django. Le navigateur ne parle **jamais** directement au backend : il reste en **same-origin** sur `/api/...`.

- L'URL cible vient de la variable d'environnement **serveur uniquement `BACKEND_BASE_URL`** (ex. dev `http://127.0.0.1:6811`). Elle **lève une erreur au runtime si absente** (fail-fast) et **n'est jamais exposée au navigateur**. Cible = `${BACKEND_BASE_URL}/api/<path>`, en préservant slash final et query string.
- **Aucune variable `NEXT_PUBLIC_*`** : le seul `process.env` du code `src/` est `BACKEND_BASE_URL` dans ce proxy.
- **Allow-list d'en-têtes** relayés en amont : `authorization`, `content-type`, `accept`, `user-agent`, `x-request-id`, `cookie`, `x-csrftoken`. Les en-têtes hop-by-hop sont retirés ; un `x-request-id` (UUID) est calculé/propagé pour la corrélation.
- **Cookies** : chaque `Set-Cookie` est réémis séparément (`getSetCookie()`) — le login pose le challenge MFA (HttpOnly) + `csrftoken`, le second facteur pose le cookie d'auth (HttpOnly) et renouvelle `csrftoken`. Redirections assainies (`isSafeLocation`).
- **Durcissement SSE** : pour les flux d'événements, pose `cache-control: no-cache` + `x-accel-buffering: no`.

Conséquence : pas de configuration d'URL backend côté client, et une surface CSRF/cookie cohérente en same-origin. Voir [../05-api/vue-ensemble.md](../05-api/vue-ensemble.md).

### Upload audio : corps streamé

Le relais `app/api/[...path]/route.ts` tamponne le corps de toute requête (`arrayBuffer()`),
SAUF pour `v1/documents/upload-audio` (`lib/proxyStreaming.ts`) : le corps y est transmis en
flux (`duplex: "half"`) avec son `Content-Length` d'origine (Django en a besoin pour parser le
multipart). Un audio de 600 Mo ne transite donc jamais entier en mémoire Node. La page
Documents (`documents/_lib/audio.ts`) envoie l'audio vers ce chemin, n'annonce les formats
audio que si l'espace a `audio_upload_enabled`, applique le plafond `ingest.audio.max_upload_bytes`
(600 000 000 octets décimaux) et affiche la sous-étape de transcription (`audio.stage`). Les
citations d'une transcription affichent « Transcription audio · 00:12:35 → 00:13:48 » sans lien
vers l'instant (pas de lecteur en V1). Voir [ingestion-audio.md](../03-backend/ingestion-audio.md).

## 4. Couche d'intégration API

### 4.1 Client fetch — `src/lib/api.ts`

Wrapper fetch **écrit à la main** — pas d'axios, pas de react-query, pas de SWR.

- `apiFetch<T>` (cœur) : toujours `credentials: "include"`, ajoute `X-CSRFToken` sur les méthodes mutantes, ajoute automatiquement le slash final sur `/api/*`, pose `Content-Type: application/json` seulement pour les corps non-FormData, gère `204/205`, normalise les messages d'erreur (`detail` / `non_field_errors`). En cas d'échec, lève une **`ApiError`** (`src/lib/apiError.ts`) porteuse du **statut HTTP** (`status`, `code`, `body`) tout en conservant le format de `message` historique. `humanAccessError()` en dérive un message **neutre** pour les refus owner-scope : **404** indistinct (anti-énumération : jamais « supprimé » vs « interdit »), **403** rôle insuffisant, sans jamais exposer de code interne. Voir [rôle effectif](#61-rôle-effectif-par-espace-owner-scope).
- Helpers : `apiGet` / `apiPost` / `apiPatch` / `apiDelete` / `apiPostForm`.
- `apiUploadForm` : utilise **`XMLHttpRequest`** (et non fetch) pour obtenir la **progression réelle d'upload** via `upload.onprogress` ; supporte `AbortSignal`.
- `apiFetchRaw` / `apiFetchBlobUrl` : binaire (visualiseur PDF → object URLs).
- Helpers de domaine typés : capabilities, résumé de document (+ polling `summarizeDocumentAwait`), liste/suppression de documents, suggestions de reformulation, `bootstrap`, chargeurs workspaces/RagSpaces, et **analyses de corpus** (create/get/cancel/poll, `TERMINAL_ANALYSIS_STATUSES`, types de recherche globale).
- `src/lib/corpus.ts` et `src/lib/coreConfig.ts` sont des clients typés additionnels bâtis sur les mêmes helpers.

### 4.2 Streaming SSE — `src/app/chat/_lib/sse.ts`

`readSseStream` **POST** un JSON avec `Accept: text/event-stream` + en-tête CSRF, puis parse manuellement le flux SSE depuis le body reader de la réponse. Il **n'utilise pas `EventSource`** car il faut POST + cookies (ce qu'`EventSource` ne permet pas). Voir [../03-backend/chat-generation.md](../03-backend/chat-generation.md).

### 4.3 Application des événements au fil — `src/app/chat/_lib/chatMessageState.ts` *(implémenté)*

Deux invariants tiennent la correction du fil, et ils sont **testés** :

- **Aucun updater `setMessages` ne mute un message existant.** Le message visé est
  reconstruit (`{ ...current, text }`), les autres sont conservés par référence.
  React StrictMode rejoue les updaters *depuis l'état de base* pour détecter
  l'impureté : avec une mutation, un `token` était concaténé deux fois et la
  réponse s'affichait en double jusqu'à l'arrivée de `done` (qui l'écrasait et
  masquait le défaut). Défaut de **développement uniquement**, jamais persisté.
- **Chaque événement vise un `local_id`**, identifiant local posé à la création
  optimiste et **jamais envoyé au backend**, au lieu de « le dernier message ».

Contrat d'application des événements, inchangé côté backend :

| Événement | Effet sur le message ciblé |
|---|---|
| `token` | **concatène** `delta` (seule opération cumulative) |
| `done` avec `answer` | **remplace** le texte par le snapshot canonique |
| `done` sans `answer` (aide, choix de corpus, garde Excel, listing) | conserve le texte accumulé |

La soumission est verrouillée par une **ref synchrone** (`submittingRef`) : `loading`
n'étant visible qu'au rendu suivant, deux soumissions du même tick le lisaient
toutes deux à `false` et déclenchaient deux générations.

## 5. Authentification

Gérée par `src/lib/auth.ts`. Modèle : **token DRF transporté par cookie HttpOnly** + CSRF double-submit.

- Le token d'auth vit dans un **cookie HttpOnly posé par le backend**, **inaccessible au JS**. **Aucun token n'est jamais stocké en `localStorage`/`sessionStorage`** ni dans un cookie lisible par JS.

  > Correction vs ancienne doc : celle-ci décrivait un « token DRF dans `localStorage` sous `ragatm_token` » — c'est **faux**. L'auth est strictement HttpOnly.

- Source de vérité de « connecté » = `GET /api/v1/auth/me/` (appelé par `/`, `/login` et `WorkspaceProvider`).
- La seule chose exposée par `auth.ts` est `getCsrfToken()`, qui lit le cookie **non sensible** `csrftoken` (pattern double-submit), renvoyé en `X-CSRFToken` sur les mutations.
- Déconnexion : `POST /api/v1/auth/logout/` en best-effort puis redirection `/login`.

### 5.1 Contrat d'erreur de la garde d'authentification — *implémenté*

Une erreur de `GET /api/v1/auth/me/` n'est **pas** une preuve de déconnexion. Seul un **401**
l'est. Le contrat est porté par `src/lib/authProbe.ts` (`guardedCall`) et appliqué par
`WorkspaceProvider` à `/auth/me/` **et** `/workspaces/` au chargement de la coquille :

| Réponse | Comportement |
|---|---|
| 200 | chargement normal |
| **401** | `router.replace("/login")` — **seul** cas de redirection |
| **429** | attente du `Retry-After` (repli **5 s** si absent ou invalide, plafond **60 s** = fenêtre du throttle DRF), puis **un seul** réessai ; second 429 → état transitoire `authIssue = rate_limited` |
| 403, 5xx | état transitoire `authIssue = unavailable`, **aucun** réessai automatique |
| erreur réseau | idem (`status` null) |

L'état transitoire s'affiche dans un bandeau (`AuthIssueNotice`, rendu par `AppShell`) qui
indique que la session est conservée et propose « Réessayer » (une tentative par clic, jamais
de boucle). Aucun `logout` n'est appelé. L'attente est annulée au démontage du provider.

Pourquoi : le throttle DRF `user` (cf. sécurité)
est **un seau unique par utilisateur pour toute l'API**. Incident staging du 08/10/2026 : une
rafale de l'onglet Espaces l'a épuisé, `/auth/me/` a répondu 429, et l'ancien `catch {}`
renvoyait vers `/login` alors que la session serveur était valide.

**Page de connexion** : un 429 (sur `POST /auth/login` ou sur le `bootstrap()` qui suit)
affiche « Trop de requêtes. Réessayez dans environ N secondes. » (N = `Retry-After`), jamais
« Identifiants invalides » ni le texte brut `API 429 …` ; pas de réessai automatique, champs
conservés. 400/401/403 et 5xx gardent leur comportement antérieur.
- Le header `Authorization: Token` reste accepté par le backend pour les clients API/tests, mais le frontend s'appuie sur le cookie.

Détails côté serveur : [../05-api/authentification.md](../05-api/authentification.md).

## 6. Gestion d'état

**Aucune bibliothèque d'état externe.** L'état = hooks React + un unique React Context.

- `src/components/shell/WorkspaceContext.tsx` — `WorkspaceProvider` + `useWorkspace()` : store applicatif. Contient `me`, `workspaces`, `workspaceId` courant, `ragSpaces`, `ragSpaceId` courant, `currentStats`, les dérivés `role`/`isAdmin`/`isGlobalOwner`/`canImport`, les dérivés de **rôle effectif par espace** (`ragSpaceAccess`, `canWriteCurrent`, `canManageCurrent`, `accessForRagSpace(id)` — cf. [§6.1](#61-rôle-effectif-par-espace-owner-scope)), et les actions (`setWorkspaceId`, `setRagSpaceId`, `upsertRagSpace`, `refreshRagSpaces`, `refreshStats`, `logout`), plus l'état transitoire `authIssue` et sa relance manuelle `retryAuth`.
- Au montage, séquence auth+bootstrap : `GET /me/` (→ `/login` **sur 401 seulement**, cf. [§5.1](#51-contrat-derreur-de-la-garde-dauthentification--implémenté)) → `bootstrap()` → `/workspaces/` → workspace mémorisé ou premier → `/rag-spaces/?workspace_id=` → RagSpace mémorisé ou premier → `/stats/` + application du thème d'accent.
- **Mapping des rôles** : rôles backend `owner`/`admin`/`member`/`viewer` → rôles produit `admin`/`editeur`/`lecteur` (`toProductRole`, `ROLE_INFO`).
- **Persistance** : uniquement des **préférences UI** en `localStorage` via `src/app/chat/_lib/storage.ts` (workspace sélectionné, map RagSpace-par-workspace, thread-par-RagSpace). La page login écrit aussi `selectedWorkspaceId`/`selectedRagSpaceId`/`bootstrapped`. **Aucune donnée sensible en `localStorage`.**
- **Brouillon du chat** *(implémenté)* : le texte saisi est un état **local** de `DraftComposer` (`src/app/chat/_components/Composer.tsx`), jamais de la page. La page ne le lit qu'à l'envoi (`onSend(text)`) et ne l'écrit que par la poignée `setValue` (vidage après envoi, commande de résumé, suggestion de reformulation recopiée). Raison : un état de page rerendait à chaque caractère tout l'historique et re-parsait chaque réponse Markdown (~250 ms par frappe sur 450 messages). Contrat inchangé : brouillon vidé dès que l'envoi part (y compris en cas d'échec réseau ensuite), conservé si l'envoi est refusé (vide, génération en cours), **conservé** au changement de fil et à la création d'une conversation (composer unique, aucune persistance par fil). Couvert par `ChatDraftIsolation`.

### 6.1 Rôle effectif par espace (owner-scope)

*Statut : implémenté.*

Le frontend adapte l'UI au **cloisonnement owner-scope** mais **ne recalcule jamais**
la politique d'autorisation : le **backend est la seule autorité**. Il expose par espace,
en lecture seule et **flag-aware**, `permission_level` (`read`/`write`/`manage`/`null`),
`space_role` et `is_owner_implicit` (cf. [../03-backend/owner-scope.md](../03-backend/owner-scope.md)).
Détail des flags : [../10-reference/feature-flags.md](../10-reference/feature-flags.md).

- **Helpers `src/lib/ragspacePermissions.ts`** : `toEffectiveAccess` (normalise la forme
  brute), `canReadRagSpace` / `canWriteRagSpace` / `canManageRagSpace`, `getRagSpaceRoleLabel`.
  Règle de sécurité : **restrictif quand inconnu** — un rôle effectif absent (`null`) n'accorde
  **aucune** capacité. On ne suppose jamais un accès à partir du seul rôle workspace.
- **Affichage** : `RagSpaceRoleBadge` (Propriétaire / Gestionnaire / Éditeur / Lecteur /
  Accès restreint) dans le `SpaceSwitcher` (espace courant + liste) et l'en-tête Documents.
- **Gating des actions** : Documents pilote import / suppression sur `canWriteCurrent` et la
  réindexation globale sur `canManageCurrent` (au lieu du seul rôle workspace). **Flags OFF ⇒
  comportement legacy inchangé** ; flags ON ⇒ précision par espace.
- **Refus** : 403/404 traités de façon centralisée via `humanAccessError` (messages neutres,
  anti-énumération, aucun code interne affiché).
- **Administration des accès** (niveau *manage*) : client `src/lib/ragspaceAccessApi.ts`
  (list / eligible / grant / change-role / revoke) et écran `AccessManagerModal`
  (onglet **Espaces** de l'admin workspace, bouton « Accès » visible seulement si
  `canManageRagSpace`). Octroi limité aux membres **éligibles du périmètre** (rôles plafonnés
  **D1** renvoyés par l'API), owner **implicite** rappelé et **non modifiable**, révocation =
  `revoked_at` côté serveur (**jamais de suppression physique**). Les permissions ne sont
  **pas persistées** côté client.

### 6.2 Console d'administration plateforme (superuser) *(implémenté)*

Route **`/platform-admin`** réservée au **superuser** (`me.is_superuser` → dérivé `isSuperuser` ;
nav `superuserOnly`). Gouvernance globale (utilisateurs, workspaces, memberships, owner-scopes,
diagnostics) **sans aucun accès au contenu**. Distincte de « Espaces & membres » (admin du
workspace courant). Client `src/lib/platformAdmin.ts` ; détails backend + doctrine :
[../03-backend/platform-admin-console.md](../03-backend/platform-admin-console.md).

## 7. Shell / mise en page

`src/components/shell/` :

- `AppShell.tsx` — enveloppe les enfants dans `WorkspaceProvider`, rend `Sidebar` + `<main>` ; exporte aussi `Topbar` (header collant). Chaque page authentifiée fait `<AppShell><XContent/></AppShell>`. Variante `<AppShell fill>` (page chat) : la coquille vaut `100dvh` et le document ne défile plus — la conversation et le panneau Sources défilent chacun seul.
- `Sidebar.tsx` — navigation + sélecteur d'espace + pied utilisateur/logout. Items **gated** par `adminOnly`/`globalOwnerOnly` : Accueil, Assistant, Documents, Corpus métier, Espaces & membres (admin), Admin corpus (propriétaire global), Admin Core (propriétaire global).
- `WorkspaceSwitcher.tsx` — **sélecteur d'espace de travail (Workspace)** : un utilisateur membre
  de plusieurs workspaces bascule entre eux (masqué si un seul). La bascule (`setWorkspaceId`) vide
  immédiatement les RagSpaces/stats du workspace précédent puis recharge ceux du nouveau. Voir
  [../03-backend/platform-admin-console.md](../03-backend/platform-admin-console.md) (accès multi-workspace).
- `SpaceSwitcher.tsx` — sélecteur de RagSpace (au sein du workspace courant).
- `ragSpaceList.ts` — `upsertRagSpaceInList` (fusion de liste idempotente).
- `breakpoints.ts` — seuils responsive + `useMediaQuery` ; `useModalLayer.ts` — calque modal des tiroirs.

### 7.1 Mise en page responsive *(implémenté, non déployé)*

Trois modes, seuils dans [../10-reference/responsive.md](../10-reference/responsive.md) :
**mobile** `≤ 767 px`, **tablette** `768–1199 px`, **desktop** `≥ 1200 px`.

- **Une seule arborescence** : la `Sidebar` et le `SourcesPanel` sont les mêmes composants dans
  tous les modes ; seul leur CSS module change (colonne fixe ↔ tiroir `position: fixed`). Fermé, un
  tiroir ne réserve aucune largeur et n'est ni visible ni tabulable (`visibility: hidden`). Aucun
  composant n'est monté en double.
- **Navigation (mobile)** : bouton menu dans la `Topbar` (`aria-expanded`, `aria-controls="app-navigation"`),
  tiroir superposé, fermeture par bouton, voile, Échap, lien suivi ou changement de page. Tablette et
  desktop : colonne de 256 px inchangée.
- **Sources (< 1200 px)** : bouton « Sources » dans la `Topbar` (`aria-controls="chat-sources"`) ; une
  citation `[n]` **sélectionne** la source, ouvre le tiroir sur le message cité et la fait défiler en
  vue. Desktop : la citation bascule la surbrillance comme avant, sans tiroir.
- **Calque modal** (`useModalLayer`) : tant qu'un tiroir est ouvert en mode compact, tout le reste
  reçoit l'attribut natif `inert` (piège de focus sans intercepteur de Tab), Échap ferme, le
  défilement du document est suspendu, et le focus revient au déclencheur (bouton ou citation). Un
  franchissement de seuil referme le tiroir et retire tout `inert`.
- **Topbar** : grille nommée (`title` / `toolbar` / `actions`). Desktop = une ligne identique à
  l'ancienne ; tablette/mobile = deux lignes stables (titre + actions primaires, puis sélecteurs de
  langue et de fil). La page chat rend ses actions en `display: contents` pour se placer dans ces zones.
  Mobile : « Nouvelle conversation » en icône seule, nom accessible conservé.
- **Clavier virtuel / encoche** : `100dvh` (repli `100vh`), export `viewport` avec
  `interactive-widget=resizes-content` et `viewport-fit=cover`, `env(safe-area-inset-*)` sur Topbar,
  Sidebar, composer et tiroir Sources. Saisie en 16 px en mobile (zoom iOS).
- **Pages secondaires** *(implémenté, non déployé — UI-02)* : `/documents` et `/platform-admin`
  débordaient par leur propre contenu (barres d'onglets en `nowrap`, en-tête
  de section). Réparation locale, sans toucher la coquille : padding de page à 16 px en mobile, onglets
  en retour à la ligne contrôlé, en-tête de section recomposé — détail dans
  [../10-reference/responsive.md](../10-reference/responsive.md#réparations-locales-de-contenu).

### 7.2 Recherche et navigation universelle *(implémenté)*

- **Sélecteur d'espace** (`SpaceSwitcher`) : champ « Rechercher un espace… » sur les espaces du
  workspace courant déjà chargés (aucun appel réseau). Classement : correspondance exacte, préfixe,
  début de mot, sous-chaîne (insensible à la casse et aux accents, `foldText`), puis favori,
  récence, nom. Sans saisie : sections **Favoris**, **Récents**, **Tous les espaces** ; bouton ★.
- **Favoris / récents** (`lib/navigationPrefs.ts`) : `localStorage` par utilisateur
  (`ragatm_nav_favorites:<userId>`, 50 max ; `ragatm_nav_recents:<userId>`, 8 max), identifiants
  seulement. Affichage = intersection avec les espaces visibles renvoyés par le backend : un espace
  supprimé ou révoqué disparaît, un renommage s'affiche. Libellé « enregistrés sur cet appareil » :
  aucune synchronisation multi-appareil.
- **Palette** (`CommandPalette`, `NavigationResults` partagé) : `Ctrl+K` / `⌘K`, `/` hors champ de
  saisie, et toujours un bouton **Rechercher** dans la Sidebar (donc dans le tiroir mobile).
  Onglets Tous / Espaces / Conversations ; espaces multi-workspace (`GET /api/v1/rag-spaces/`,
  workspaces actifs) ; conversations par `GET /api/v1/chat/threads/search/` après 2 caractères et
  250 ms (requête précédente annulée, « Afficher plus » par curseur). Combobox ARIA
  (`aria-activedescendant`), Échap et restitution du focus via `useModalLayer`, plein écran
  ≤ 767 px. Un **429** de recherche affiche « trop de recherches » : jamais de déconnexion ni de
  nouvel essai automatique (la recherche ne passe pas par la sonde d'authentification).
- **Lien direct** `/chat?thread=<uuid>` : la page résout le fil (`GET /api/v1/chat/threads/<uuid>/`),
  active workspace et espace (`openTarget`), puis charge par le chemin habituel `/messages/`. Un
  résultat de conversation est un vrai lien (Ctrl+clic, clic milieu). Refus ou id invalide :
  bandeau « Cette conversation n'existe pas ou ne vous est plus accessible », aucun titre ni contenu
  divulgué, sélection courante conservée. L'URL suit le fil courant (`history.replaceState`).
- **Au-delà de 100 conversations** : le menu des fils propose « Afficher plus de conversations »
  (`GET /api/v1/chat/threads/?before=<uuid>`), et un fil ouvert par lien reste sélectionné même
  hors de la première page.

## 8. Conventions UI

- **Design system** dans `src/components/ui/` avec un barrel `index.ts` : `Icon` (+`RG_PATHS`/`IconName`), `Button`, `Field`, `Card`, `Avatar` (+`initialsFrom`), `StatusBadge`/`Pill`/`RG_STATUS` (+`DocStatus`), `EmptyState`, `Modal`, `Popover`, `Logo`/`SectionLabel`.
- **Pattern Popover portal** (`src/components/ui/Popover.tsx`) — conforme à la note de mémoire projet : **tout menu flottant/dropdown passe par ce composant**, qui `createPortal` vers `document.body` en `position: fixed`. Cela échappe à tout `overflow:hidden`/contexte d'empilement parent (motif : des menus en `position:absolute` dans des cartes `overflow:hidden` étaient tronqués). Il gère le positionnement par rect d'ancre, le flip haut/bas selon l'espace, la fermeture clic-extérieur + Échap, et le repositionnement au scroll/resize. **Convention : jamais de dropdown en `position:absolute` dans un conteneur overflow.**
- **Style** majoritairement **inline + variables CSS** (design tokens dans `src/styles/tokens.css` : `--ink`, `--accent-soft`, `--surface`, `--shadow-pop`…). Couleur d'accent **par RagSpace** appliquée dynamiquement via `src/lib/theme.ts` (`accentForId`/`applyAccent`). CSS modules réservés à ce qui exige une media query ou un pseudo-élément : `AssistantMarkdown.module.css` et la mise en page responsive (`AppShell`, `Sidebar`, `ChatLayout`, `SourcesPanel`, `Composer`) — pas de migration générale du style inline. `styled-jsx` est actif (config babel vitest) et utilisé dans certains composants. Langue : **français** partout.
- Helpers : `src/lib/format.ts` (dates, tailles, noms), `src/types/api.ts` + `src/types/openapi.ts` (formes API typées).

## 9. Flux utilisateur principaux

- **Page de connexion — scène du pipeline** *(implémenté)* → la colonne droite de `/login`
  affiche `src/components/login/PremiumRagPipelineHero.tsx` : une scène **isométrique en SVG**
  (vrais prismes à trois faces ombrées, géométrie pure dans `heroGeometry.ts`) qui montre
  documents → ingestion / vision → embeddings / indexation → recherche → reranking → LLM →
  réponse sourcée, dans un espace à accès contrôlé. Le LLM est placé **après** la sélection
  des passages, jamais comme source. Purement **illustrative** : aucun appel réseau, aucune
  donnée réelle, aucun timer ni écouteur ; aucune dépendance graphique ajoutée (≈ +5,6 kB
  sur la route `/login`). Animation CSS seule (boucle de 14 s : `opacity`, `transform`,
  `stroke-dashoffset`) ; sous `prefers-reduced-motion`, composition fixe complète. Scène
  `aria-hidden`, remplacée pour les lecteurs d'écran par une description textuelle
  (`login.heroA11y`) ; aucun élément focalisable. Textes dans `messages/{fr,en}/login.json`
  (clés `hero*`). En dessous de 1180 px, libellés agrandis et détails secondaires masqués ;
  en dessous de 860 px, la colonne droite reste masquée (inchangé). Le formulaire, la MFA et
  leurs appels ne sont pas touchés. Tests : `tests/frontend/components/PremiumRagPipelineHero.test.tsx`.
- **Connexion** → `src/app/login/page.tsx` : `POST /api/v1/auth/login` (backend pose cookies HttpOnly + `csrftoken`) → `bootstrap()` → préférences → `/chat`. `/` et `WorkspaceProvider` gèrent aussi le gating via `/me/`.
- **Sélection workspace / RagSpace** → `WorkspaceContext` + `SpaceSwitcher` dans la sidebar ; sélection persistée par workspace en `localStorage`.
- **Gestion RagSpace** → onglet `spaces` de `/workspace-admin` + `SpaceEditModal` (créer/éditer) ; `upsertRagSpace`/`refreshRagSpaces` pour l'UI optimiste.
- **Configuration IA d'un Space** *(implémenté)* → `SpaceEditModal` consomme
  `GET /api/v1/ai/model-catalog/` (`src/lib/modelCatalog.ts`). **Aucune liste de providers
  ni de modèles n'est déclarée dans le composant** — c'est verrouillé par
  `SpaceEditModalNoHardcodedModel.test.ts`. Points structurants :
  - **Chat → « Moteur »** (`Local`, seul moteur : modèle local servi par l'AI Gateway), plus
    « Provider ». Le choix est persisté en `llm_provider` (`ollama`).
  - **Routage affiché, non éditable** : « Routage : AI Gateway ». Ni IP, ni port, ni URL
    interne — un test vérifie leur absence du DOM.
  - **Modèle → `select`** alimenté par le catalogue, `allowed` uniquement — pour le chat,
    seuls les modèles `generation_status=supported` (servis par `/v1/generate`). Un modèle
    `available` mais non autorisé n'est pas sélectionnable ; `recommended` et `qualified`
    sont des **badges textuels** (jamais la couleur seule). Un modèle persisté devenu
    inutilisable reste affiché avec un avertissement motivé (non servi / raisonnement
    exposé / non vérifié / retiré) et n'est jamais remplacé ; l'enregistrement est bloqué
    seulement si le modèle de chat **change** vers une valeur non sélectionnable. États
    explicites : aucun modèle compatible, défaut de la plateforme indisponible.
  - Test de la garde de génération :
    `tests/frontend/components/SpaceEditModalGenerationGate.test.tsx`.
  - **Embeddings → « Pipeline »** et non provider technique : `legacy-768` (recommandé),
    `fr-en-retrieval` (qualifié, latence plus élevée). Dimension et modèle effectif
    affichés ; le digest n'est jamais un choix.
  - **Espaces historiques** : un espace en `ollama`/`nomic-embed-text` s'affiche
    « Legacy — nomic-embed-text » et n'est **jamais** converti implicitement en
    `legacy-768`, même si le modèle amont est le même — l'identité vectorielle diffère.
  - **Avertissement de réindexation chiffré**, et surtout **séparant** les documents
    prêts (qui seront marqués) des documents en échec (qui ne seront pas réparés). Les
    additionner produit le compteur trompeur observé sur l'espace AEC.
  - **Catalogue indisponible** → champs désactivés, configuration courante toujours
    visible, **jamais de retour au champ libre**.
  - Le backend reste autoritaire : le filtrage du formulaire ne remplace pas la
    validation (cf. [../06-ai/providers-modeles.md](../06-ai/providers-modeles.md) §5.3).
- **Upload de documents** → `/documents` : upload unitaire via `apiUploadForm` (progression XHR), **import de dossier** via `_components/FolderImportModal` + `_lib/folderImport.ts` + `_lib/uploadScheduler.ts` (concurrence bornée `FOLDER_UPLOAD_CONCURRENCY` **et** cadence bornée `UPLOAD_MIN_INTERVAL_MS`, avec pause coordonnée sur HTTP 429 — voir [import-cadence-et-reprise.md](import-cadence-et-reprise.md)) ; au plus **1 000 fichiers compatibles** et 1 Gio par import de dossier (bornes et interaction avec le quota : [../10-reference/quotas-workspace.md](../10-reference/quotas-workspace.md#bornes-dun-import-de-dossier-frontend)). Taille max **50 Mio** (alignée backend, `UPLOAD_MAX_FILE_SIZE_MB`). Statut via `StatusBadge`/`toDocStatus`. Les formats proposés ne sont plus une liste du frontend : ils viennent **intégralement** d'`ingest.formats` et `ingest.image_formats` de `/api/v1/capabilities/` (voir [../03-backend/ingestion-extraction.md](../03-backend/ingestion-extraction.md)).
- **Sélection des formats à l'import de dossier** *(implémenté)* → après le choix du dossier, `FolderImportModal` propose **une case à cocher par format fonctionnel** (PDF, Word, Excel, PowerPoint, Markdown, HTML, CSV, Texte, Images) ; seuls les formats **réellement présents** dans le dossier sont listés, et **tous sont cochés par défaut**. Décocher un format recalcule le récapitulatif : les fichiers correspondants ne sont **pas envoyés**. Points structurants :
  - **Source unique** : `FORMAT_CATALOG` (`_lib/types.ts`) traduit une **clé de format backend** en extensions ; `uploadAccept(msg, formats, imageFormats)` ne retient que les clés annoncées par `ingest.formats`. Le socle de seize extensions n'est **plus statique** : fermer `DOCUMENT_EXCEL_ENABLED` retire `.xlsx` de la modale, au lieu de laisser le backend refuser fichier par fichier. La famille `image` tire ses extensions d'`ingest.image_formats`, en conservant les alias (`jpeg` → `.jpg`/`.jpeg`, `tiff` → `.tif`/`.tiff`). Une clé annoncée hors catalogue reste déposable sous son propre nom — jamais silencieusement perdue. Conséquence voulue : **ouvrir un format côté serveur ne demande plus aucun patch frontend**. « PowerPoint » ne couvre toujours que `.pptx` (jamais `.ppt`/`.pptm`), « Excel » que `.xlsx`. Le NOMMAGE de la modale d'import vit dans `FORMAT_BY_EXTENSION` (`_lib/folderImport.ts`) : les formats sous capacité serveur y sont nommés (« Outlook (.msg) », « IFC »…) pour ne pas s'afficher sous le repli par extension. Nommer n'autorise rien — `ingest.formats` reste seul à décider — et toute extension non nommée garde son repli, donc reste sélectionnable.
  - **Rien n'est filtré avant le choix** : la page conserve les fichiers bruts (`folderFiles`) et recalcule `buildFolderPlan(files, { maxSizeBytes, selectedExtensions })` à chaque bascule.
  - **Motifs d'exclusion distincts** : `office_temporary_file` (verrou Office) ≠ `unsupported` (hors catalogue effectif) ≠ `format_not_selected` (décoché) ≠ `too_large` ≠ `empty`.
  - **Fichiers sans extension** : un fichier nommé `PLAN` n'est plus classé « format non supporté ». Le frontend ne peut pas connaître son format ; le backend le décide **par signature**. Il est donc admis sous un format DISTINCT (`UNKNOWN_FORMAT_KEY`, libellé « Sans extension »), décochable comme les autres, et **jamais affiché comme PDF** avant détection serveur. Ce n'est pas un relâchement : le backend refuse exactement comme avant — il est simplement consulté. Si le serveur n'annonce aucun format, ces fichiers restent écartés.
- **Fichiers temporaires Office ignorés** *(implémenté)* → les fichiers dont le nom commence par `~$` (`~$rapport.docx`) sont des **verrous** créés par Word/Excel à l'ouverture d'un document, pas des documents : ils sont écartés **avant tout appel réseau**, sur tous les chemins d'import (dossier, sélection multiple, glisser-déposer). Points structurants :
  - **Règle canonique unique** : `isOfficeTemporaryFilename` (`_lib/folderImport.ts`) — préfixe `~$` sur le **basename**, jamais sur le chemin. `sous-dossier/~$a.docx` est un verrou ; `rapport~$.docx` n'en est pas un.
  - **Ni une erreur, ni un format non supporté** : ils portent l'extension du document (`.docx`, `.xlsx`) mais ~160 octets de nom de session. Motif dédié, ton neutre — rien à corriger côté utilisateur.
  - **Exclus des compteurs par format** : un dossier contenant `budget.xlsx` et `~$budget.xlsx` annonce « Excel — 1 fichier ». Un format présent uniquement sous forme de verrous n'est pas proposé.
  - **Défense backend** : `validation.py` les refuse sous le code `office_temporary_file` (400, avant toute création de `Document`). Le frontend n'est pas une barrière — il évite l'aller-retour, le backend décide.
  - **Présélection réinitialisée** à chaque nouvelle sélection de dossier ; sélection vide ⇒ import désactivé.
  - **Import strictement additif** : le payload reste `file` + `rag_space_id` + `relative_path` + `source_folder` ; aucun mode remplacement/synchronisation, aucune suppression. La déduplication par SHA-256 côté backend est inchangée (voir [../05-api/reference-endpoints.md](../05-api/reference-endpoints.md)).
- **Chat** → `/chat` : `Composer` → `buildChatRequestBody` (`chatRequest.ts`) → `readSseStream` (SSE) → rend `AssistantMarkdown`, `SourcesPanel`, `MessageActions`, `RephrasingSuggestions`, `CorpusScopeChoice` (portée privée vs corpus), et `AnalysisCard` pour les analyses de corpus (pollées via `analysisPoll.ts` ; reprises via `getThreadAnalyses`). Résumés via `summaryIntent.ts` + `summarizeDocumentAwait`.
  - **Rendu Markdown mémoïsé** *(implémenté)* : `AssistantMarkdown` est enveloppé dans `React.memo` (prop `content` = chaîne, comparaison superficielle exacte) et passe une liste `REMARK_PLUGINS` constante. Chaque rendu re-parse tout le texte (micromark + GFM + mdast→hast→JSX) : sans cette frontière, tout rerendu de la page (clic de citation, mise à jour d'analyse, jeton de streaming) re-parsait chaque réponse de l'historique. Mesuré sur 450 messages : clic de citation p95 299 → 46 ms. Couvert par `AssistantMarkdownMemo`.
  - **Lignes de message mémoïsées** *(implémenté)* : `Message` (`page.tsx`) est enveloppé dans `React.memo`. Pendant un flux, chaque jeton crée un nouveau tableau `messages` mais réutilise les objets inchangés : sans cette frontière, toutes les lignes de l'historique se rerendaient à chaque jeton (~45 ms par jeton sur 450 messages, dont ~9 ms de next-intl). Les callbacks des lignes sont **stables** (`onRowCite(index, n)`, `onRowCorpusChoice`) et délèguent, au moment du clic, aux gestionnaires du **dernier** rendu (`rowHandlers`, ref réassignée à chaque rendu) : mode compact, fil et état d'envoi courants, jamais ceux du rendu de la ligne. L'index est une prop, pas une capture. Une ligne se rerend encore quand **ses** props changent (sa citation sélectionnée, son analyse, `canAsk` au début et à la fin d'un flux). Couvert par `ChatStreamingRowIsolation`.
  - **Lignes de message confinées** *(implémenté)* : la racine de chaque ligne `Message` porte la classe `messageRow` (`ChatLayout.module.css`), `contain: layout style`. Sur un long fil presque toutes les lignes sont hors écran ; sans frontière, le navigateur reparcourait leurs propriétés de peinture (PrePaint) à chaque image où le composer ou le message streamé change. **Ni `paint` ni `content-visibility`** : le confinement de peinture rogne l'anneau de focus des actions au bord de la ligne, et `content-visibility: auto` retire le contenu hors écran de l'arbre d'accessibilité et fausse la géométrie du fil. Amélioration progressive : un navigateur sans `contain` garde le comportement actuel. Couvert par `ChatRowContainment` (classe + garde de source) ; effets de rendu vérifiés au banc navigateur.
  - **Citation d'une diapositive** (`sources.ts`) : « Diapositive N » vient de `meta.slide_number`, jamais de `page_number`. Un fragment **visuel** de présentation (image intégrée analysée) porte `slide_number` sans `kind` natif (`srcIsSlideVisual`) : la pastille s'affiche, mais `srcViewerHref` ne propose **aucun** lien d'aperçu — l'aperçu de présentation rejoue des positions de texte natif que ce fragment n'a pas (voir [../03-backend/apercu-presentation.md](../03-backend/apercu-presentation.md)).
  - **Citation d'un courriel** (`sources.ts`) : `srcEmailSubject` (objet du courriel parent) et `srcEmailAttachment` (pièce jointe, seulement si `meta.email_part === "attachment"` — jamais déduite d'un nom) ; `SourcesPanel` affiche « Email : … » puis « Pièce jointe : … », puis le repère interne (page, diapositive, feuille). Pour une pièce, `srcViewerHref` n'ajoute **aucun** `page=` : la page appartient à la pièce, pas au courriel parent. Les documents classiques sont inchangés. Résumé des pièces dans la notice d'extraction : `emailAttachmentSummary` (interrogeables / sans texte exploitable / non prises en charge / refusées / en échec / limite). Texte d'aide du dépôt : `emailFormatsLabel` — une seule mention pour `.msg` et `.eml`, pièces jointes annoncées seulement si `ingest.msg.attachment_ingestion` est vrai (jamais Vision, GIF, antivirus ni calcul tabulaire). Voir [../03-backend/ingestion-email.md](../03-backend/ingestion-email.md).
- **Visualiseur de document** → `/doc/[documentId]` : **routage par type** avant tout téléchargement. PDF en blob avec deep-link de page ; classeur `.xlsx` en aperçu tabulaire borné (feuille + plage citées) ; tout autre format en état explicite avec téléchargement. Détail : [visualiseur-documents.md](visualiseur-documents.md).
- **Corpus métier** → `/corpora` (consommateur, lecture seule) et `/ragatm-admin` (CRUD propriétaire global : domaines/corpus/docs + affectation workspace), tous deux via `src/lib/corpus.ts`. Voir [../03-backend/corpus-metier.md](../03-backend/corpus-metier.md).

## 10. Tests

> Correction vs ancienne doc : les tests UI **existent** (« pas de tests UI » était faux).

- **Runner : Vitest 4.1.12** (Vite 7.3.8 ; `frontend/vitest.config.ts`), environnement jsdom, globals actifs, `@vitejs/plugin-react` avec `styled-jsx/babel`, `css: false`, alias `@ → src`, `dir: ../tests/frontend` avec motifs relatifs (l'exclusion `**/node_modules/**` doit couvrir le lien `tests/frontend/node_modules`).
- **Les tests vivent HORS du package frontend**, dans `tests/frontend/` (soit `frontend/../tests/frontend/**/*.test.{ts,tsx}`). Setup `tests/frontend/setup.ts` (matchers jest-dom, cleanup RTL, purge `localStorage`, polyfill `scrollIntoView`).
- Comme les tests sont hors de `frontend/`, `scripts/link-test-deps.mjs` (exécuté par `pretest`) crée un symlink `tests/frontend/node_modules → frontend/node_modules` pour résoudre les imports « bare ». `vitest.config.ts` whiteliste le dossier parent (`server.fs.allow`).
- **Bibliothèques** : `@testing-library/react` ^16.3.2, `/dom`, `/jest-dom`, `/user-event`, `jsdom` ^25.
- **Couverture : 55 fichiers de test** (26 `lib/`, 29 `components/`), répartis en :
  - `tests/frontend/lib/` (unitaire) : `api`, `auth`, `appRouterExports`, `chatRequest`, `chatMessages`, `chatMessageState`, `sse`, `sources` (+ Excel), `storage`, `suggestions`, `summaryIntent`, `folderImport`, `uploadScheduler`, `uploadExcel`, `analysisPoll`, `rephrasingPoll`, `corpusAnalysis`, `coreConfig`, `ragSpaceList`, `humanError`, `action_intents` ;
  - `tests/frontend/components/` (RTL) : `AnalysisCard`, `AnalysisRealPayload`, `AnalysisResume`, `AssistantMarkdown`, `AssistantMarkdownMemo`, `ChatDraftIsolation`, `ChatRowContainment`, `ChatStreamingRowIsolation`, `ChatStreamingStrictMode`, `ChatTableStreaming`, `Composer`, `CorpusScopeChoice`, `DocumentSnippet`, `DocumentToolbar`, `FolderImportModal`, `HelpMarkdown`, `MessageActions`, `RephrasingSuggestions`, `SidebarAdminCore`, `SourcesPanel`, `SpaceEditModal`, `WorkspaceContextSpaces`, `WorkspaceContextAuth`, `LoginRateLimit`, `SpacesTabStatusLoop`, `AdminCoreView`.
  - `ChatStreamingStrictMode` monte la page de chat **en `reactStrictMode`** et coupe le flux après `token` : c'est le seul test qui observe l'état réellement affiché pendant la génération, donc le seul capable d'attraper une régression d'immuabilité (hors StrictMode, il passerait à tort).
  - `appRouterExports` balaie tous les fichiers de route de `src/app/` et vérifie qu'ils n'exportent que les champs autorisés par Next (export par défaut + configuration de segment ; verbes HTTP pour un `route.ts`). Un export nommé dans un `page.tsx` n'échoue **que** pendant `next build` — ni `next dev`, ni `tsc`, ni les tests ne le voient.
  - `ResponsiveShell` (tiroir de navigation : aria, `inert`, focus, Échap, voile, lien, changement de page, absence d'effet en desktop) et `ChatSourcesDrawer` (bouton Sources, citation → tiroir sur la source, retour du focus, desktop inchangé) simulent la largeur par un `matchMedia` factice : ils vérifient l'**état**, jamais des dimensions (jsdom ne calcule aucune mise en page).
  - Fixture : `fixtures/globalSearchResult.json`.
- Accent mis sur la logique chat/analyses/sources/upload. **Aucun e2e/Playwright** versionné : la mise en page responsive a été qualifiée par un banc navigateur hors dépôt (Chromium headless sur `next start`, API interceptée), dont les mesures sont rapportées dans lors d'une qualification antérieure. Voir [../08-testing/strategie.md](../08-testing/strategie.md).

## 11. Legacy et points d'attention

- **`action_intents.test.ts`** teste du vrai code front : il exerce `parseSummaryIntent` (`src/app/chat/_lib/summaryIntent.ts`, la pré-détection UX de résumé) contre le contrat inter-langage `shared/contracts/action_intents/summarize.json` (un pendant Python valide le côté backend). Ce n'est **pas** du code mort. Le routeur `apps.actions` lui-même reste une fonctionnalité **backend seulement** (mémoire projet : derrière `ACTIONS_ROUTER_ENABLED`, off par défaut), sans module front dédié.
- **Analyse de corpus « Analyse complète »** : les commentaires de `src/lib/api.ts` indiquent que l'option « analyse complète » du chat est encore **non branchée**/fondationnelle (jalons L2/L3/L4). Le client API (create/get/cancel/global-search) existe **en avance** sur une UI pleinement câblée — **non branché**, pas mort.
- **`public/`** ne contient qu'un `README.md` (aucun asset statique/favicon livré).
- **Stubs README** : plusieurs `README.md` (`src/`, `src/app`, `src/components`, `src/lib`, `src/styles`, `src/types`) sont des notes d'échafaudage, parfois périmées — **doc périmée**, pas du code.
- **`@types/react` en v19** alors que le runtime React est en 18 : dérive de types (`strict:false` masque l'essentiel).
- `.pytest_cache/` sous `frontend/` est un artefact parasite (le front utilise vitest) — inoffensif.

## 12. Configuration

| Variable | Rôle |
|---|---|
| `BACKEND_BASE_URL` | URL du backend Django (serveur uniquement, **requise** au runtime du proxy). Dev : `http://127.0.0.1:6811` |
| `PORT` | Port Next.js. Dev RagATM : `6300` (`-H 127.0.0.1 -p 6300`) |
| `NODE_ENV` | `production` en prod |

Aucune variable `NEXT_PUBLIC_*`. Détails : [../10-reference/variables-environnement.md](../10-reference/variables-environnement.md), politique des ports : [../10-reference/ports.md](../10-reference/ports.md).

## Références d'implémentation

- `frontend/package.json`, `frontend/next.config.mjs`, `frontend/tsconfig.json`
- `frontend/src/app/api/[...path]/route.ts` (reverse-proxy)
- `frontend/src/lib/api.ts`, `frontend/src/lib/apiError.ts`, `frontend/src/lib/auth.ts`, `frontend/src/lib/corpus.ts`, `frontend/src/lib/coreConfig.ts`, `frontend/src/lib/theme.ts`, `frontend/src/lib/format.ts`
- Owner-scope (rôle effectif) : `frontend/src/lib/ragspacePermissions.ts`, `frontend/src/lib/ragspaceAccessApi.ts`, `frontend/src/components/ui/RoleBadge.tsx`, `frontend/src/app/workspace-admin/_components/AccessManagerModal.tsx`, `frontend/src/app/workspace-admin/_lib/spaceRoles.ts`
- `frontend/src/app/chat/_lib/sse.ts`, `frontend/src/app/chat/_lib/chatRequest.ts`, `frontend/src/app/chat/_lib/chatMessages.ts`, `frontend/src/app/chat/_lib/chatMessageState.ts`, `frontend/src/app/chat/_lib/storage.ts`, `frontend/src/app/chat/_lib/summaryIntent.ts`
- `frontend/src/lib/authProbe.ts`, `frontend/src/components/shell/AuthIssueNotice.tsx`
- `frontend/src/components/shell/WorkspaceContext.tsx`, `frontend/src/components/shell/AppShell.tsx` (+ `.module.css`), `frontend/src/components/shell/Sidebar.tsx` (+ `.module.css`), `frontend/src/components/shell/SpaceSwitcher.tsx`, `frontend/src/components/shell/breakpoints.ts`, `frontend/src/components/shell/useModalLayer.ts`
- Responsive chat : `frontend/src/app/chat/_components/ChatLayout.module.css`, `SourcesPanel.module.css`, `Composer.module.css`
- `frontend/src/components/ui/` (barrel `index.ts`, `Popover.tsx`), `frontend/src/styles/tokens.css`
- `frontend/src/app/{page,login,home,chat,documents,doc/[documentId],corpora,workspace-admin,ragatm-admin,admin-core}/`
- `frontend/vitest.config.ts`, `frontend/scripts/link-test-deps.mjs`, `tests/frontend/`
