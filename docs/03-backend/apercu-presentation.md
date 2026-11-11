# Aperçu de présentation — visualiser la source d'une citation PPTX

Objectif : décrire l'endpoint qui rend une **fenêtre de diapositives** autour du
passage cité, sans jamais envoyer la présentation entière ni prétendre reproduire
PowerPoint.
Public : développeurs backend. Source de vérité = le code.

> **Statut** : *implémenté*, **activable par flag** — `DOCUMENT_PPTX_PREVIEW_ENABLED`.
> Fermé, un PPTX retombe sur l'état explicite (nom, format, téléchargement).

Côté interface : [../04-frontend/visualiseur-documents.md](../04-frontend/visualiseur-documents.md).
Aperçu documentaire (DOCX) : [apercu-documentaire.md](apercu-documentaire.md).
Aperçu tabulaire (XLSX) : [apercu-tabulaire.md](apercu-tabulaire.md).
Ingestion : [ingestion-extraction.md](ingestion-extraction.md).

---

## 1. Un pipeline déjà bien outillé

Contrairement au DOCX, le rail PPTX porte déjà l'essentiel :

| Acquis | Conséquence |
|---|---|
| **Une diapositive = une unité de chunking**, jamais fusionnée | un chunk ne traverse **jamais** deux diapositives |
| `slide_number` (1-based) **persisté et projeté** | le frontend affiche déjà « Diapositive N » |
| `slide_title` persisté | inventaire lisible sans heuristique |
| `EXTRACTOR_VERSION` / `CHUNKING_VERSION` **existent déjà** | garde de version gratuite |
| `notes_included` persisté | garde de configuration possible |
| Tableaux rendus en **Markdown déterministe** | échappement du `|`, lignes paddées |
| Ordre de lecture **recalculé** (sémantique → bandes visuelles → ordre XML) | déterministe, pas le z-order |
| Test de référence figeant la sortie **octet pour octet** | oracle supplémentaire gratuit |

```
GET /api/v1/documents/{document_id}/presentation-preview/
    ?chunk_id=…&slide_number=4&context_before=1&context_after=1
```

---

## 2. Les deux pièges, et leurs gardes

Ce sont les deux raisons pour lesquelles un aperçu naïf afficherait un
surlignage **faux sans que rien n'échoue**.

### Piège 1 — `res.text` n'est pas le texte des offsets

Deux règles coexistent dans le dépôt :

| Règle | Où | Rôle |
|---|---|---|
| `global_offset += len(text.strip()) + 1` | `indexing.py` (`_build_slide_specs`) | **définit** `char_start` / `char_end` |
| `res.text = "\n\n".join(...)` | `pptx_extractor.py` | définit `Document.meta.extraction.chars` |

Les offsets vivent donc dans un texte **virtuel** à séparateur d'**un** caractère,
qui n'est matérialisé nulle part. Appliquer `res.text[char_start:char_end]`
dérive de **+1 caractère par unité déjà traitée** — invisible sur la première
diapositive, franc au milieu d'un deck.

> `res.text` est **proscrit** de ce chemin. Un test dédié
> (`TestResTextInterdit`) échoue si la règle de séparation change, et vérifie que
> l'écart vaut exactement un caractère par unité.

### Piège 2 — l'ensemble des unités dépend de la configuration

Les notes du présentateur produisent des unités **intercalées** entre les
diapositives. Les activer décale donc toutes les bases suivantes — mesuré :
**+299, +598, +897** caractères sur quatre diapositives.

Un chunk indexé notes activées, relu notes désactivées, serait surligné ~900
caractères plus loin. La garde est donc obligatoire :

```
Document.meta.extraction.pptx.notes_included  ==  réglage courant   → OK
                                              !=                    → notes_configuration_mismatch
```

**Aucune compensation n'est tentée** : on ne peut pas deviner la longueur de
notes qui n'ont pas été extraites. Le refus est la seule réponse honnête.

---

## 3. Séparation des autorités

| Question | Autorité |
|---|---|
| Quel document ? | `document_id` |
| Quelle version ? | `Document.sha256` + `extractor_version` + `chunking_version` |
| Quelle diapositive ? | `Chunk.meta.slide_number` (**1-based**) |
| Quelle sous-plage ? | `Chunk.meta.char_start` / `char_end` |
| Quelle règle d'offset ? | cumul `len(unit.text.strip()) + 1` |
| Quel réglage de notes ? | `Document.meta.extraction.pptx.notes_included` |
| Quel texte afficher ? | le **même extracteur** que l'ingestion |

**Le snippet n'est jamais analysé.** Il n'entre dans aucune décision.

---

## 4. Fidélité — deux invariants testés

**Invariant 1 — les offsets reconstruisent le chunk :**

```
unit_text[char_start - unit.start : char_end - unit.start].strip() == chunk.text
```

**Invariant 2 — le surlignage reconstruit le chunk** : la concaténation des
fragments marqués, séparés par `\n\n`, est exactement `chunk.text`.

Vérifiés **sous les deux réglages de notes** et sur trois configurations de
chunking, sur un corpus couvrant listes imbriquées, tableau à cellule vide,
zones de texte en ordre visuel non trivial, texte alternatif, Unicode/CJK et
**texte répété à deux positions** — celui qu'une recherche de snippet
confondrait.

### Le surlignage est au caractère

Les chunks sont découpés **sans égard pour les frontières de blocs** :

```
highlight_start = max(char_start, block_start) - block_start
highlight_end   = min(char_end,   block_end)   - block_start
```

Marquer un bloc entier étendrait la citation à du texte qu'elle ne porte pas.

---

## 5. Blocs — ce qui est rendu, et ce qui est déclaré

Le texte d'une unité est assemblé par `"\n\n"` et **aucune partie n'en contient
elle-même** — vérifié sur le corpus de référence. Le découpage est donc exact et
réversible, et `split_blocks` le **contrôle** : une divergence lève au lieu de
décaler.

Les types sont déduits des **préfixes que le rendu pose lui-même**, jamais d'une
heuristique de mise en forme :

| Préfixe | Type | Fidélité déclarée |
|---|---|---|
| `Titre : …` | `title` | placeholder canonique `PP_PLACEHOLDER.TITLE` |
| `Tableau :\n…` | `table` | `table_simplified`, `merged_cells_may_repeat` |
| `- …` | `list` | `list_numbering_lost` |
| `[Image : …]` | `paragraph` + `alt_text` | texte **réellement indexé**, pas un placeholder |
| `[Diapositive N]` | `paragraph` + `provenance_marker` | marqueur injecté dans le texte indexé, donc citable |
| unité de notes | `speaker_notes` | homogène |

### Pourquoi les tableaux ne sont pas des `<table>`

Le Markdown est pourtant bien formé et échappé — meilleur qu'en DOCX. Mais
python-pptx **répète** la valeur d'une cellule fusionnée sur toute son étendue
(choix assumé de l'extracteur : « une valeur répétée est moins trompeuse qu'un
trou »), et la matrice de cellules n'est nulle part persistée. Reconstruire une
grille exigerait de **re-parser notre propre rendu** — un aller-retour fragile
pour une structure que la donnée ne porte pas.

### Pourquoi le marqueur de provenance est affiché

`[Diapositive N]` fait partie du **texte indexé** : il occupe ses caractères et
peut être couvert par un chunk. Le masquer décalerait les offsets. Il est donc
rendu, mais visuellement atténué — il n'est pas du discours de l'auteur.

---

## 6. Ce qui n'est pas affiché — et pourquoi

| Élément | État | Traitement |
|---|---|---|
| **Images** | non lues (ni relation, ni blob) | non rendues ; la **présence** est signalée via `has_images` / `requires_visual_check`, qui sont des faits persistés |
| **Fragments visuels** (images intégrées analysées) | chunk sans `kind` natif, `slide_number` au premier niveau | **non prévisualisables** : l'aperçu rejoue des positions de texte natif qu'ils n'ont pas → refus `slide_not_previewable` ; jamais de positions inventées, jamais présentés comme `pptx_slide`. Le panneau Sources affiche « Diapositive N » sans lien d'aperçu. La ré-extraction de l'aperçu n'active jamais la découverte visuelle |
| **Graphiques, SmartArt** | **muets à l'extraction, sans warning** | déclarés absents dans `fidelity.warnings` — sans quoi une diapositive qui en porte paraîtrait vide |
| **Diapositives masquées** | `show="0"` **jamais lu** | affichées comme les autres, **sans badge inventé** ; déclaré `hidden_slides_not_detected` |
| **Notes** | unités séparées, flag **fermé par défaut** | affichées **seulement si indexées**, dans une zone distincte |
| Animations, transitions, vidéos, OLE | jamais lus ni exécutés | déclarés omis |
| Positions, couleurs, polices, thèmes | `top`/`left` servent au tri, jamais exportés | déclarés omis |

> **Diapositives masquées — doctrine assumée.** L'information n'existe pas : le
> viewer ne peut pas distinguer masqué de visible. Inventer un badge serait faux
> une fois sur deux ; masquer ces diapositives rendrait une source indexée
> inaccessible. On les affiche donc normalement et on **déclare la limitation**.
> La lecture de `show="0"` relève du lot 2.

---

## 7. Bornes et mesures

| Borne | Défaut | Maximum |
|---|---|---|
| `PPTX_PREVIEW_CONTEXT_BEFORE` / `_AFTER` | 1 / 1 | 5 / 5 |
| `PPTX_PREVIEW_MAX_SLIDES` | 10 | — |
| `PPTX_PREVIEW_MAX_BLOCKS_PER_SLIDE` | 200 | — |
| `PPTX_PREVIEW_MAX_BLOCK_CHARS` (affichage) | 2 000 | — |
| `PPTX_PREVIEW_MAX_TOTAL_CHARS` (**cardinale**) | 400 000 | — |
| `PPTX_PREVIEW_MAX_INVENTORY` | 500 | = limite d'ingestion |

Banc : `python manage.py bench_presentation_preview --skip-checks`.

| Cas | Slides | Blocs | Caractères | p50 | p95 | Pic RSS | Fenêtre |
|---|---|---|---|---|---|---|---|
| 10 diapositives | 10 | 30 | 4 902 | 54 ms | 55 ms | 0,2 Mo | 2,9 Ko |
| **100 diapositives** | 100 | 300 | 49 184 | **450 ms** | 450 ms | 0,6 Mo | 2,9 Ko |
| 300 diapositives | 300 | 900 | 147 984 | 1 344 ms | 1 354 ms | 2,1 Mo | 2,9 Ko |
| **500 (limite d'ingestion)** | 500 | 1 500 | 246 784 | **2 276 ms** | 2 305 ms | 2,8 Mo | 2,9 Ko |
| 100 slides, formes nombreuses | 100 | 300 | 182 384 | 511 ms | 517 ms | 0,9 Mo | 6,8 Ko |
| 100 slides, tableaux nombreux | 100 | 267 | 39 584 | 407 ms | 407 ms | 0,7 Mo | 2,5 Ko |
| 100 slides, notes activées | 200 | 500 | 64 444 | 569 ms | 577 ms | 1,8 Mo | 1,9 Ko |

Décomposition à 500 diapositives : extraction **2 294 ms** · bases **4,9 ms** ·
inventaire **0,6 ms** · projection **0,1 ms**.

Deux enseignements :

1. **Le coût est celui de l'extraction**, pas de l'aperçu — la logique propre au
   viewer pèse ~5 ms sur 2 300. La ré-extraction est complète parce que les bases
   d'offset dépendent de **toutes** les unités précédentes ; cibler une seule
   diapositive supposerait de persister leurs longueurs (lot 2).
2. **La fenêtre bornée évite l'essentiel de la charge utile** : 2,9 Ko contre
   241 Ko pour le texte entier d'un deck de 500 diapositives.

Mémoire stable : 30 ouvertures successives donnent une dérive de **+1,67 Mo**.

---

## 8. Codes de refus

**Aucune erreur n'est convertie en résultat vide**, et une diapositive sans texte
n'est pas une erreur : elle rend `200` avec `blocks: []` et le warning
`slide_has_no_indexable_text`.

| `reason_code` | HTTP | Sens |
|---|---|---|
| `not_found` | 404 | inexistant **ou** hors périmètre (indiscernables) |
| `no_file_available` | 404 | fichier absent du stockage |
| `chunk_document_mismatch` | 404 | chunk d'un autre document / RagSpace |
| `slide_not_found` | 404 | diapositive inconnue |
| `chunk_stale` | 409 | chunk supprimé — présentation réindexée |
| `document_version_mismatch` | 409 | version d'extracteur, de chunking, ou `sha256` |
| **`notes_configuration_mismatch`** | 409 | réglage des notes différent de l'ingestion |
| `unsupported_format` | 422 | pas un `.pptx` |
| `slide_not_previewable` | 422 | chunk d'un autre rail |
| `presentation_unreadable` | 422 | archive ou XML invalide — **jamais un 500** |
| `encrypted_presentation` | 422 | protégée par chiffrement DRM/IRM |
| `preview_unavailable` | 422 | métadonnées d'extraction absentes |
| `invalid_chunk_offsets` | 422 | offsets absents ou incohérents |
| `presentation_too_large` | 413 | au-delà des bornes d'ingestion |
| `invalid_slide_range` | 400 | diapositive contradictoire ou invalide |
| `context_limit_exceeded` | 400 | contexte excessif |
| `preview_timeout` | 504 | budget dépassé |

---

## 9. Sécurité

- **Permission d'abord.** `load_accessible_document` — la même porte que le
  téléchargement — avant lecture de fichier, de cache, de chunk.
- **Le chunk et les gardes AVANT le cache.** Sans cela un aperçu survivrait à la
  réindexation qui a supprimé son chunk, ou à un changement de réglage des notes.
  Deux tests le verrouillent.
- **IDOR** : appartenance au document **et** au RagSpace vérifiées.
- **Gardes ZIP/XML réutilisées** : anti zip-bomb, `presentation.xml` exigé,
  parties > 8 Mo refusées, `DOCTYPE`/`ENTITY` refusés avant tout parsing,
  chiffrement détecté (`encrypted_pptx`), `.pptm` refusé **par contenu** même
  renommé.
- **Profondeur XML bornée** — `reject_deeply_nested_xml` (200 niveaux) existait
  depuis le lot DOCX mais **n'était pas câblée sur le rail PPTX** : quelques
  kilo-octets d'XML très imbriqué épuisent la pile de lxml, et aucune garde de
  **taille** ne le voit. Elle est désormais appliquée à `presentation.xml` et à
  chaque diapositive. La correction ne change le texte d'aucun document valide.
- **Relations externes** : `.rels` n'est jamais lu, aucun média n'est ouvert.
- **Macros, OLE, vidéos** : jamais lus ni exécutés.
- **Contenu de bloc** : transporté comme texte, échappé par le rendu React.
- **Journalisation** : identifiants, diapositive, bornes, durée, nombre de blocs.
  **Jamais** de texte de diapositive — vérifié par un test à canari.

---

## 10. Hors périmètre et suites

**Lot 2 — extraction** (change `res.text` ⇒ **bump de version + réindexation
contrôlée**) : lecture de `show="0"` et badge des diapositives masquées ; texte
des graphiques et du SmartArt ; `buAutoNum` ; cellules de tableau structurées et
fusions ; **persistance des longueurs d'unités** (débloque l'extraction ciblée
d'une seule diapositive) ; warnings d'extraction ; `res.partial` sur profondeur
de groupe ; unification de la règle de séparation entre `res.text` et les offsets.

**Lot 3 — tableaux structurés et images** : matrice de cellules et fusions avant
tout `<table>` ; relations, position, MIME allowlisté, taille bornée, texte
alternatif et endpoint média protégé avant toute image.

**Exclus durablement** : rendu spatial, miniatures serveur, conversion PDF,
LibreOffice, Office Online / Microsoft Graph, animations, transitions, vidéos,
objets OLE, thèmes, pixel-perfect.

---

## Références d'implémentation

- `backend/apps/rag_core/services/preview/presentation_service.py` (orchestration)
- `backend/apps/rag_core/services/preview/presentation_blocks.py` (bases +1, blocs)
- `backend/apps/rag_core/services/preview/{config,errors}.py`
- `backend/api/v1/documents/views_presentation_preview.py`, `backend/api/v1/urls.py`
- `backend/apps/rag_core/services/ingest/extraction/pptx_extractor.py`
  (garde de profondeur XML câblée)
- `backend/apps/rag_core/management/commands/bench_presentation_preview.py`
- Tests : `tests/tests_root/unit/preview/test_presentation_preview_fidelity.py` (oracle),
  `tests/tests_root/http_api/test_documents_presentation_preview.py`,
  `tests/tests_root/integration/test_pptx_source_to_preview.py` (bout-en-bout)
