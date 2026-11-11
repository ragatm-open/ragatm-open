# Points de rupture responsive (frontend)

Référence des seuils de mise en page du frontend. **Source unique** : les autres docs pointent ici.
Le CODE fait foi : `frontend/src/components/shell/breakpoints.ts` et les media queries des
CSS modules qui en recopient les valeurs (une media query ne peut pas lire une variable).

Statut : *implémenté*. *Non déployé* en staging ni en production
à la date d'écriture : qualifié localement (build de production + navigateur réel).

---

## Seuils

| Mode | Largeur (CSS px) | Navigation | Panneau Sources (chat) | Conversation |
|---|---|---|---|---|
| Mobile | `≤ 767` | tiroir superposé (bouton menu dans la Topbar) | tiroir plein écran, à la demande | pleine largeur |
| Tablette | `768 – 1199` | colonne fixe `256 px` | tiroir latéral `min(380px, 100%)`, à la demande | `viewport − 256` |
| Desktop | `≥ 1200` | colonne fixe `256 px` | colonne fixe `340 px` | `viewport − 596` (inchangé) |

Constantes TypeScript : `MOBILE_MAX_WIDTH = 767`, `COMPACT_MAX_WIDTH = 1199`
(`MOBILE_QUERY`, `COMPACT_QUERY`). Elles ne servent qu'au **comportement** (refermer un tiroir au
franchissement d'un seuil, ouvrir le tiroir Sources depuis une citation) ; la **mise en page**
est entièrement en CSS.

Origine des valeurs : mesures lors de la qualification (chat utilisable ≥ ~320 px,
confortable ≥ ~600 px ; à 1024 px trois colonnes laissaient 428 px de conversation).

## Autres règles

| Règle | Valeur | Portée |
|---|---|---|
| Police du champ de saisie | `16 px` en mobile, `15 px` au-delà | évite le zoom automatique d'iOS au focus |
| Cible tactile minimale | `44 × 44 px` | bouton menu, fermetures de tiroir, bouton Sources, Envoyer, Nouvelle conversation, déconnexion, actions des cartes Sources (mobile) |
| Pastilles de citation `[n]` | visuel `18 × 18`, zone cliquable étendue de `13 px` (vertical) et `6 px` (horizontal) | `≤ 1199 px` |
| Hauteur de la page chat | `100dvh` (repli `100vh`) | coquille `AppShell fill` |
| Viewport | `width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content` | `frontend/src/app/layout.tsx` |
| Zones sûres | `env(safe-area-inset-*)` sur Topbar, Sidebar, composer, tiroir Sources | nul hors appareils à encoche |

## Réparations locales de contenu

*Implémenté, non déployé*. Même seuil mobile que le shell, aucun
nouveau point de rupture ; chaque page garde son CSS module.

| Page | Cause | Correction |
|---|---|---|
| `/documents` | 5 filtres en `nowrap` (≈ 393 px) + padding de page 32 px | filtres en `flex-wrap`, padding 16 px en mobile |
| `/platform-admin` | 4 onglets avec icônes (≈ 454 px) ; manquait aussi 8 px à 768 | onglets en `flex-wrap` (2 + 2 sous ~490 px de contenu), padding 16 px en mobile |

Stratégie retenue pour les onglets : retour à la ligne contrôlé plutôt qu'un défilement horizontal
local, qui aurait caché le dernier onglet sans signal visible.

## Références d'implémentation

- `frontend/src/components/shell/breakpoints.ts`, `frontend/src/components/shell/useModalLayer.ts`
- `frontend/src/components/shell/AppShell.tsx` + `AppShell.module.css`, `Sidebar.tsx` + `Sidebar.module.css`
- `frontend/src/app/chat/_components/ChatLayout.module.css`, `SourcesPanel.module.css`, `Composer.module.css`
- `frontend/src/app/layout.tsx` (export `viewport`)
- Réparations locales : `frontend/src/app/documents/_components/DocumentsLayout.module.css`,
  `frontend/src/app/platform-admin/_components/PlatformAdmin.module.css`,
