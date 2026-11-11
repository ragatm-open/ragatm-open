# Visualiseur de documents — routage par type

Objectif : décrire le **comportement réel** de `/doc/[documentId]` selon le format du
document ouvert depuis une source du chat.
Public : développeurs frontend et backend. Source de vérité = le code.

> **Statut** : *implémenté, activé par défaut*. Aucun flag ne gouverne ce routage —
> il n'existe pas de chemin par lequel l'ancien comportement (iframe pour tout
> format) puisse revenir.

Contrat d'aperçu tabulaire : [../03-backend/apercu-tabulaire.md](../03-backend/apercu-tabulaire.md).
Contrat d'aperçu documentaire : [../03-backend/apercu-documentaire.md](../03-backend/apercu-documentaire.md).
Contrat d'aperçu de présentation : [../03-backend/apercu-presentation.md](../03-backend/apercu-presentation.md).
Endpoints : [../05-api/reference-endpoints.md](../05-api/reference-endpoints.md).

---

## 1. Le défaut corrigé

Le visualiseur était **implicitement PDF**. Il téléchargeait tout document en `Blob`,
annonçait `Accept: application/pdf,*/*`, et posait le blob dans une `<iframe>`.

Pour un PDF, le lecteur natif du navigateur rend la page. Pour un classeur `.xlsx`,
aucun navigateur ne sait rendre le type MIME
`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` : la zone restait
**blanche**, sans message — indiscernable d'un échec de chargement.

Trois faits, tous vérifiés dans le code d'origine, se conjuguaient :

1. `apiFetchBlobUrl` **retournait** le `contentType` de la réponse ; la page le **jetait** ;
2. `chunk_id` était écrit dans l'URL par le chat (`srcViewerHref`) et **jamais lu** ;
3. aucune branche par type n'existait — un seul chemin, calibré pour le PDF.

---

## 2. Le routage

La page interroge d'abord `GET /api/v1/documents/<id>/` (endpoint **préexistant**),
lit le champ `preview_kind`, puis choisit le visualiseur. **Aucun octet du fichier
n'est téléchargé avant que le type ne soit connu.**

| `preview_kind` | Visualiseur | Comportement |
|---|---|---|
| `pdf` | `PdfDocumentViewer` | Blob + `<iframe>` + ancre `#page=N` — **inchangé** |
| `spreadsheet` | `SpreadsheetDocumentViewer` | Aperçu tabulaire HTML borné, feuille et plage citées |
| `document` | `DocumentPreviewViewer` | Aperçu documentaire structuré (DOCX) : titres, paragraphes, listes aplaties, tableaux préformatés |
| `presentation` | `PresentationPreviewViewer` | Aperçu par diapositive (PPTX) : inventaire, blocs, notes séparées, tableaux préformatés |
| `text` | `UnsupportedDocumentViewer` | État explicite (aperçu texte non livré dans ce lot) |
| `unsupported` | `UnsupportedDocumentViewer` | État explicite + téléchargement |

### Où la décision est prise

**Dans le backend**, `api/v1/documents/preview_kind.py`. Deux raisons :

- **une seule source de décision** — dupliquer la table des types dans le frontend
  les ferait diverger au premier format ajouté ;
- **le backend connaît le type réel** — `Document.content_type` est posé à
  l'ingestion par une détection **par contenu** (`extraction/mime.py`), jamais d'après
  l'extension déclarée par le client.

Le frontend garde un repli défensif (`_lib/previewKind.ts`) pour un backend antérieur
à ce champ, mais il ne **décide** pas. Le repli respecte la même doctrine : le type
MIME prime, l'extension ne sert que si le type est absent ou vaut `octet-stream` —
jamais pour contredire un type connu.

### CSV : délibérément `unsupported`

Le CSV est bien **ingéré et interrogeable**, mais ses chunks ne portent **aucune
localisation structurée** : le rail CSV passe par le chunker texte générique, sans
feuille, sans plage, sans numéro de ligne persisté. Le visualiseur tabulaire ne
saurait pas où surligner. L'annoncer comme pris en charge serait un mensonge ; il
tombe donc dans l'état explicite, avec téléchargement de l'original.

---

## 3. Invariant : aucune zone blanche

Tout état de la page rend quelque chose de lisible et d'actionnable :

| État | Rendu |
|---|---|
| Métadonnées en cours de chargement | « Chargement du document… » (`aria-live`) |
| `404` / hors périmètre | « Ce document est introuvable, ou vous n'y avez pas accès » + téléchargement + retour |
| `401` | redirection `/login` |
| Erreur réseau | message explicite + téléchargement + retour |
| Format non prévisualisable | nom, format, localisation connue, téléchargement, retour |
| Aperçu tabulaire en échec | **repli** sur le même état explicite |

`UnsupportedDocumentViewer` est le **repli permanent** : il affiche toujours au moins
un nom de fichier, une raison, et un moyen d'agir.

---

## 4. Terminologie

Deux libellés étaient implicitement PDF et ont changé :

| Avant | Après | Motif |
|---|---|---|
| « Télécharger le PDF » | « Télécharger l'original » | le bouton sert tous les formats |
| « Page — » quand `page` est nul | *aucune ligne* | un classeur n'a **pas** de pages (`page_number` est `None` à l'ingestion Excel : une feuille n'est pas une page). Le libellé de remplissage laissait croire à une pagination absente |

Pour un classeur, la barre d'outils affiche à la place :

```
Feuille : Proposition liste données princ · Cellules : A749:D761
```

Le test qui verrouillait « Page — » a été **réécrit**, pas supprimé : il vérifie
désormais qu'aucune ligne de page n'est rendue sans pagination.

---

## 4bis. Le visualiseur de classeur

### Ouverture sur la source

Le frontend ne devine ni la feuille, ni la plage : il transmet le **`chunk_id`** reçu
dans l'URL, et le backend résout la localisation depuis `Chunk.meta`. Le texte du
snippet n'entre dans aucune décision.

### Trois niveaux de lecture, visuellement distincts

| Niveau | Rendu |
|---|---|
| **passage cité** | fond ambré + bordure, `aria-current` |
| **zone de contexte** | neutre, autour de la citation |
| **reste de la feuille** | non chargé — la fenêtre est bornée côté serveur |
| **autres feuilles** | onglets, dont celui portant la citation est badgé « source » |

Le surlignage porte sur **chaque plage citée**, jamais sur leur rectangle englobant :
pour un chunk découpé par colonnes, celui-ci marquerait les colonnes intermédiaires,
qui ne font pas partie de la source.

### Navigation entre feuilles

Changer d'onglet relance une requête bornée **sans `chunk_id`** : on consulte alors une
autre feuille, et il n'y a plus de citation à surligner (« Aucune citation sur cette
feuille »). Revenir sur la feuille source réinjecte le `chunk_id` et restaure le
surlignage.

Transmettre le `chunk_id` en consultant une autre feuille produirait un
`sheet_reference_conflict` — le backend refusant d'arbitrer entre la feuille demandée
et celle de la citation. C'est voulu : ce refus est ce qui garantit qu'aucune feuille
ne s'affiche « à la place » de la source.

Les feuilles `veryHidden` n'apparaissent pas : la liste vient du backend et reflète la
politique d'**ingestion**. Le composant ne filtre rien et ne doit rien compléter.

### Transparence

Un bandeau de fidélité est **toujours** affiché : valeurs et coordonnées restituées,
styles / images / graphiques absents, aucune formule recalculée. Toute troncature est
annoncée avec son motif, et une cellule tronquée porte sa longueur réelle en infobulle.

### Sécurité du rendu

Les valeurs sont du contenu **non fiable**. Elles sont rendues comme nœuds texte React,
qui échappe par construction : aucun `dangerouslySetInnerHTML`, aucun lien activé,
aucune interprétation Markdown. Un `<script>` dans une cellule s'affiche littéralement.
Les amorces de formule (`=`, `+`, `-`, `@`) arrivent déjà neutralisées par la
normalisation d'ingestion.

---

## 5. Composants

```
src/app/doc/[documentId]/
├── page.tsx                          routeur : métadonnées → preview_kind → viewer
├── _lib/previewKind.ts               typage + repli défensif
└── _components/
    ├── DocumentToolbar.tsx           titre, contexte (page OU feuille+plage), actions
    ├── DocumentSnippet.tsx           extrait cité (inchangé)
    ├── PdfDocumentViewer.tsx         blob + iframe + #page=N (comportement historique)
    ├── SpreadsheetDocumentViewer.tsx aperçu tabulaire — états, navigation
    ├── SpreadsheetSheetTabs.tsx       onglets des feuilles indexées
    ├── SpreadsheetGrid.tsx            grille, surlignage, accessibilité
    ├── DocumentPreviewViewer.tsx      aperçu DOCX — sommaire, fenêtre, états
    ├── DocumentPreviewBlock.tsx       bloc + surlignage au caractère
    ├── PresentationPreviewViewer.tsx  aperçu PPTX — diapositives, notes, états
    ├── PresentationBlock.tsx          bloc de diapositive + surlignage
    └── UnsupportedDocumentViewer.tsx état explicite — repli permanent
```

### Stabilité des rappels

Les effets de chargement ne dépendent **pas** des rappels `onUnauthorized` /
`onOpenHrefChange` : ceux-ci dérivent de `useRouter()`, dont l'identité n'est pas
garantie stable. Les inscrire en dépendances relançait l'effet à chaque rendu et
bloquait l'écran sur « Chargement… ». Ils sont gardés en `useRef` ; l'effet ne
dépend que du document demandé.

---

## Références d'implémentation

- `frontend/src/app/doc/[documentId]/page.tsx` (routage)
- `frontend/src/app/doc/[documentId]/_lib/previewKind.ts` (repli défensif)
- `frontend/src/app/doc/[documentId]/_components/` (visualiseurs)
- `backend/api/v1/documents/preview_kind.py` (décision canonique)
- `backend/api/v1/documents/views_common.py` (`_serialize_document`)
- Tests : `tests/frontend/components/DocumentViewerRouting.test.tsx`,
  `tests/frontend/components/DocumentPreviewKind.test.ts`,
  `tests/frontend/components/DocumentToolbar.test.tsx`,
  `tests/tests_root/http_api/test_documents_preview_kind.py`,
  `tests/frontend/components/SpreadsheetDocumentViewer.test.tsx`

---

## 6. Le visualiseur documentaire (DOCX)

### Ce qu'il montre

Le **contenu indexé**, pas le fichier. Une fenêtre bornée de blocs autour du
passage cité : titres, paragraphes, listes **aplaties**, tableaux **préformatés**.

Le frontend transmet le `chunk_id` ; le backend convertit les offsets persistés
(`char_start` / `char_end`) en blocs et sous-plages. Le snippet n'entre dans
aucune décision — ce qui compte d'autant plus qu'un document répète couramment le
même texte à plusieurs endroits.

### Surlignage au caractère

Les chunks sont découpés sans égard pour les frontières de blocs : le premier et
le dernier bloc d'une citation sont **partiellement** couverts. Le rendu découpe
donc `(avant, cité, après)` et n'entoure que la partie citée d'un `<mark>`.
Marquer le bloc entier étendrait la citation à du texte qu'elle ne porte pas.

### Navigation

Sommaire des titres détectés, affiché **seulement s'il existe**. Cliquer un titre
recharge une fenêtre bornée autour de son bloc, sans `chunk_id` — rien n'est alors
surligné, et un bouton « Revenir au passage cité » rétablit la citation.

Quand aucun titre n'est détecté, l'interface le dit : *« Aucun sommaire fiable n'a
été détecté pour ce document. »* La détection repose sur une heuristique de style
fragile (voir la doc backend) ; la présenter comme fiable serait un mensonge.

**Aucune navigation par page** : les pages Word ne sont pas une donnée du fichier,
et les sauts de page explicites eux-mêmes sont perdus à l'extraction.

### Rendu et sécurité

Les niveaux de titre sont bornés 1–6 à l'affichage et annoncés par
`role="heading"` + `aria-level` — pas par un `h1..h6` dynamique : le niveau vient
d'une heuristique, et un plan de titres faux serait annoncé comme structurel aux
lecteurs d'écran.

Le contenu est rendu en nœuds texte React, `white-space: pre-wrap` pour préserver
tabulations et sauts de ligne. Aucun `dangerouslySetInnerHTML`, aucun lien activé,
aucune liste ou table sémantique fabriquée. Un `<script>` s'affiche littéralement,
y compris à l'intérieur d'un surlignage.

### États

Chargement · prêt · **document vide** (état explicite, pas une erreur) · chunk
périmé · version incompatible · offsets invalides · document illisible · trop
grand · timeout · introuvable · erreur réseau. Aucun ne produit de zone blanche :
tous retombent sur l'état explicite avec téléchargement et retour.

---

## 7. Le visualiseur de présentation (PPTX)

### Ce qu'il montre

Le **contenu indexé**, diapositive par diapositive. L'unité de navigation est la
diapositive, numérotée **1-based** — la même convention que l'ingestion et que la
pastille « Diapositive N » déjà affichée dans le panneau de sources.

Le frontend transmet le `chunk_id` ; le backend résout la diapositive et projette
les offsets persistés sur les blocs. Le snippet n'entre dans aucune décision.

### Navigation

Inventaire complet des diapositives (numéro + titre, ou « Diapositive N » à
défaut), boutons précédent / suivant, et badge « source » sur la diapositive
citée. Cliquer une autre diapositive recharge **sans `chunk_id`** — plus rien
n'est surligné — et « Revenir à la source » réinjecte le chunk.

### Notes du présentateur

Affichées **uniquement si elles ont été indexées**, dans une zone visuellement
séparée du corps, avec le badge « contenu indexé séparément ». Une note n'est
jamais mêlée au contenu visible : c'est un commentaire d'orateur, pas le contenu
publié.

### Contenu visuel

Aucune image n'est rendue et **aucun placeholder n'est inventé**. En revanche,
lorsque `has_images` ou `requires_visual_check` le dit — ce sont des faits
persistés — la diapositive porte une mention : *« Cette diapositive contient des
éléments visuels non affichés dans cet aperçu. »* Une valeur **absente** signifie
inconnu, pas faux : on n'affirme alors rien.

### Rendu et sécurité

Tableaux en `<pre>` avec mention des fusions possiblement répétées ; listes à plat
sans `<ul>`/`<ol>` fabriqués ; titres annoncés par `role="heading"` sans niveau de
plan inventé. Contenu rendu en nœuds texte React, `white-space: pre-wrap`. Aucun
`dangerouslySetInnerHTML`, aucun lien activé, aucune image créée depuis le
contenu.

### États

Chargement · prêt · diapositive sans texte · chunk périmé · version incompatible ·
**réglage des notes changé** · offsets invalides · diapositive inconnue ·
présentation chiffrée · illisible · trop grande · timeout · introuvable · erreur
réseau. Aucun ne produit de zone blanche.
