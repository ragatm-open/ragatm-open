# Aperçu tabulaire — visualiser la source d'une citation Excel

Objectif : décrire l'endpoint qui rend une **fenêtre de cellules** autour de la plage
citée par une source de chat, sans jamais charger le classeur entier ni recalculer
quoi que ce soit.
Public : développeurs backend. Source de vérité = le code.

> **Statut** : *implémenté, activé par défaut*. Aucun flag ne gouverne l'endpoint ;
> ses bornes sont surchargeables (`PREVIEW_*`).

Côté interface : [../04-frontend/visualiseur-documents.md](../04-frontend/visualiseur-documents.md).
Ingestion et métadonnées : [ingestion-extraction.md](ingestion-extraction.md).
Bornes : [../10-reference/variables-environnement.md](../10-reference/variables-environnement.md).

---

## 1. Le besoin

Une réponse RAG cite « 1 284 350 € ». L'utilisateur veut vérifier **d'où vient ce
chiffre**. Le télécharger l'oblige à ouvrir Excel, chercher la feuille, chercher la
ligne — et le doute reste entier jusque-là.

L'aperçu répond à cette question précise : montrer les cellules citées, surlignées,
avec quelques lignes de contexte. Ce n'est **pas** un tableur, ni un explorateur de
classeur.

```
GET /api/v1/documents/{document_id}/tabular-preview/
    ?chunk_id=…&sheet_index=2&range=A749:D761&context_rows=15&context_columns=3
```

---

## 2. Séparation des autorités

C'est la décision structurante. Trois questions, trois sources de vérité — jamais
confondues :

| Question | Autorité | Pourquoi |
|---|---|---|
| **Où est la source ?** | `Chunk.meta` | c'est la localisation sur laquelle la citation a été produite ; le fichier ne peut pas la contredire |
| **Que contient cette zone ?** | fichier source relu, **normalisation rejouée** | seul moyen d'afficher la chaîne indexée tout en offrant du contexte hors chunk |
| **Est-ce toujours la même version ?** | `Document.sha256` | invalidation binaire, déjà en base et indexée |

**Le texte du snippet n'est jamais analysé.** Il n'entre dans aucune décision.

---

## 3. Fidélité — la contrainte la plus forte

L'ingestion **ne recopie pas** les cellules brutes : elle les normalise
(`excel_normalize.render_cell`).

| Cellule | Fichier | Indexé | Un aperçu naïf afficherait |
|---|---|---|---|
| `0.1` + format `0%` | `0.1` | **`10%`** | `0.1` ❌ |
| date sérielle `44927` | `44927` | **`2023-01-01`** | `44927` ❌ |
| texte `+1+1` | `+1+1` | **`'+1+1`** | `+1+1` ❌ (injection) |
| texte `00123` | `00123` | `00123` | `123` ❌ |

Un visualiseur qui affiche une valeur différente de celle sur laquelle la citation a
été produite est **incorrect**. L'aperçu rejoue donc la normalisation canonique, avec
la **même limite de caractères** qu'à l'ingestion (`DOCUMENT_EXCEL_MAX_CELL_CHARS`).
Aucun second formateur n'existe dans le chemin d'affichage.

### L'oracle de non-divergence

Le test cardinal ne compare pas à des valeurs écrites à la main — ce serait vérifier
le test contre lui-même. Il compare, **caractère par caractère**, les cellules rendues
par l'aperçu à celles reconstruites depuis les chunks réellement indexés par
`stable_source_resolver.extract_region_cells`.

Seule exception admise : la **troncature d'affichage** (`PREVIEW_MAX_CELL_CHARS`,
500). Elle est alors déclarée par le drapeau `truncated` **et** par `full_length`, qui
donne la longueur réelle. Une troncature muette mentirait sur la donnée ; une
troncature dite est une simplification assumée.

---

## 4. Localisation et plages non contiguës

`sheet_index` est **1-based** — convention de `SheetInfo.index`, déjà en base. C'est
la clé **canonique** : deux feuilles nommées `Data` et `Data ` sont distinctes dans
Excel mais collisionnent après normalisation (NFC + trim externe). Un index ne
collisionne jamais. Le nom sert à l'affichage et au **contrôle de cohérence**.

### Le piège du découpage par colonnes

Une ligne trop large est découpée en fenêtres de colonnes. Chaque sous-chunk porte la
**colonne d'identification reportée** plus une fenêtre contiguë — soit une zone
**non contiguë**. Or `cell_range` ne décrit que la fenêtre :

```
cell_range   = "M749:P749"                    ← incomplet
data_ranges  = ["A749:A749", "M749:P749"]     ← la vérité
```

Surligner `cell_range` seul omettrait l'identifiant : une citation incomplète
présentée comme complète. `data_ranges` a donc été **ajouté à l'allowlist**
`source_meta` (avec `identity_columns`, `identity_range`, `chunk_axis`,
`column_part_index/count`) et fait foi quand il est présent.

Le surlignage porte sur **chaque plage**, jamais sur leur rectangle englobant : celui-ci
marquerait les colonnes intermédiaires, qui ne font pas partie de la citation. Le
rectangle englobant sert uniquement à **cadrer** la fenêtre.

---

## 5. Feuilles — l'inventaire suit la politique d'ingestion

La liste **n'est pas** `wb.sheetnames`.

| État | Dans l'aperçu | Motif |
|---|---|---|
| `visible` | ✅ | cas nominal |
| `hidden` | ✅ avec son état | indexée par la politique `hidden_only` |
| `veryHidden` | ❌ **absente et inaccessible** | **non indexée** : l'exposer ferait apparaître dans l'interface RAG un contenu que le RAG a délibérément écarté |
| feuille-graphique | ❌ | pas une grille, rien à afficher |

Une demande directe d'une feuille non consultable rend `sheet_not_previewable`, jamais
un repli.

> **Granularité de permission** : RagATM ne connaît pas de permission par feuille. La
> plus fine s'arrête au RagSpace, et `Document`/`Chunk` en héritent. L'accès au
> document donne accès à ses feuilles **indexées** — ce qui est cohérent, puisque
> l'utilisateur peut déjà télécharger le classeur entier. La frontière que l'aperçu
> respecte est celle de l'**indexation**, pas une permission.

---

## 6. Bornes et coût

Le coût d'openpyxl en `read_only` est **linéaire dans l'index de la ligne visée** :
atteindre la ligne 19 000 impose de parser les 18 999 précédentes. Mesuré : ~1,1 à
4,6 s selon le classeur. Conséquences directes :

- la deadline est vérifiée **pendant** l'itération (toutes les 256 lignes), pas après —
  sinon elle ne bornerait rien ;
- la feuille n'est **jamais** matérialisée (`grid = [list(r) for r in ws.iter_rows()]`
  est proscrit) ;
- le passage est **unique** : la ligne d'en-tête est captée au vol pendant la traversée
  obligatoire (+134 ms mesurés) plutôt que par une seconde ouverture, qui coûterait un
  re-parsing complet.

| Borne | Défaut | Maximum |
|---|---|---|
| `PREVIEW_CONTEXT_ROWS_DEFAULT` / `_MAX` | 15 | 50 |
| `PREVIEW_CONTEXT_COLUMNS_DEFAULT` / `_MAX` | 3 | 10 |
| `PREVIEW_MAX_ROWS` | 300 | — |
| `PREVIEW_MAX_COLUMNS` | 60 | — |
| `PREVIEW_MAX_CELLS` (**cardinale, prime**) | 10 000 | — |
| `PREVIEW_MAX_CELL_CHARS` (affichage) | 500 | — |
| `PREVIEW_TIMEOUT_SECONDS` / `_HARD_` | 5 s | 10 s |
| `PREVIEW_CACHE_ENABLED` / `_TTL_SECONDS` | on / 300 s | — |

Quand la borne cardinale mord, ce sont les **lignes** qui sont rognées, jamais les
colonnes : perdre une colonne casserait l'alignement valeur/en-tête, perdre une ligne
ne casse rien. Toute réduction est déclarée dans `truncation`.

Le classeur est de toute façon déjà borné à l'ingestion (`DOCUMENT_EXCEL_MAX_SHEETS`
100, `MAX_COLUMNS` 256, `MAX_CELLS` 1e6).

### Mesures

Banc reproductible : `python manage.py bench_tabular_preview --skip-checks`.
Classeurs générés en mémoire, aucun binaire commité.

| Cas | Feuilles | p50 | p95 | Pic mémoire |
|---|---|---|---|---|
| 200 × 10, début de feuille | 1 | 19 ms | 20 ms | 0,9 Mo |
| 200 × 10, fin de feuille | 1 | 67 ms | 69 ms | 0,9 Mo |
| 5 000 × 20, début | 1 | 29 ms | 29 ms | 1,0 Mo |
| 5 000 × 20, milieu | 1 | 1 413 ms | 1 435 ms | 1,2 Mo |
| 5 000 × 20, fin | 1 | 2 798 ms | 2 835 ms | 1,3 Mo |
| 2 000 × 10, **20 feuilles** | 20 | 372 ms | 384 ms | 4,9 Mo |
| 50 000 × 20 (**1 M cellules**), fin | 1 | 29 549 ms | 31 298 ms | 4,9 Mo |

Deux enseignements :

1. **Le coût suit l'index de la ligne, pas la taille de la fenêtre.** Une fenêtre de
   21 lignes coûte 29 ms au début d'une feuille et 2 798 ms à sa fin. C'est le
   streaming XML d'openpyxl, pas le rendu.
2. **La mémoire reste plate** (≤ 5 Mo) quelle que soit la taille du classeur, et ne
   dérive pas : 30 ouvertures successives donnent **−0,20 Mo** entre le premier et
   le dernier tiers. Le nombre de feuilles pèse plus que le nombre de lignes.

> ⚠️ **Limite connue.** Une citation située en fin d'un classeur proche du million de
> cellules **n'est pas prévisualisable** : la lecture atteindrait ~30 s, la deadline
> l'interrompt à 5,01 s et l'endpoint rend `preview_timeout`. Le worker n'est jamais
> monopolisé, et l'utilisateur reçoit l'état explicite avec téléchargement de
> l'original — jamais une zone blanche. C'est une limite assumée du MVP : la lever
> supposerait un index de positions de lignes, hors périmètre de ce lot.

---

## 7. Formules

L'aperçu affiche la **valeur mise en cache**, jamais le texte de la formule, et ne
recalcule **rien** (`data_only=True`). Aucune macro n'est lue ni exécutée — `.xlsm` est
refusé par contenu dès l'ingestion.

| Cas | Rendu |
|---|---|
| formule + valeur en cache | la valeur normalisée |
| formule **sans** cache | cellule vide **+ drapeau `uncached_formula`** |
| erreur `#N/A`, `#VALUE!`, `#REF!` | le code d'erreur, drapeau `error_cell` |
| classeur à recalculer | avertissement `workbook_requires_recalculation` |
| lien externe | jamais suivi (`keep_links=False`) |

Le cas « formule sans cache » mérite l'attention : lue en `data_only=True`, elle rend
`None` — **exactement comme une cellule vide**. Sans drapeau, l'aperçu afficherait un
blanc et laisserait croire à une absence de donnée. La liste vient de
`Document.meta.excel.uncached_formula_cells`, persistée à l'ingestion par un scan XML :
aucune seconde ouverture du classeur n'est nécessaire. Cette liste est **plafonnée** à
l'ingestion — un classeur qui en compte davantage n'en verra qu'une partie signalée.
C'est une perte de signalement, jamais une valeur fausse.

---

## 8. Codes de refus

**Aucune erreur n'est convertie en résultat vide.** Une grille sans lignes et « je n'ai
pas pu lire » sont deux réponses différentes ; les confondre recréerait la zone blanche
que ce lot supprime, déplacée d'un cran.

| `reason_code` | HTTP | Sens |
|---|---|---|
| `not_found` | 404 | document inexistant **ou** hors périmètre (indiscernables) |
| `no_file_available` | 404 | fichier absent du stockage |
| `chunk_document_mismatch` | 404 | le chunk appartient à un autre document / RagSpace |
| `sheet_not_found` | 404 | feuille inconnue du classeur |
| `chunk_stale` | 409 | chunk supprimé — le document a été réindexé |
| `document_version_mismatch` | 409 | version attendue différente, ou feuille renommée |
| `unsupported_format` | 422 | pas un `.xlsx` (CSV inclus) |
| `chunk_not_tabular` | 422 | source PDF / PPTX / e-mail |
| `sheet_not_previewable` | 422 | `veryHidden` ou feuille-graphique |
| `workbook_unreadable` | 422 | classeur corrompu — **jamais un 500** |
| `chunk_location_invalid` | 400 | méta de chunk absente ou hors convention |
| `sheet_reference_conflict` | 400 | index et nom se contredisent |
| `invalid_cell_range` | 400 | plage mal formée |
| `range_out_of_bounds` | 400 | plage hors dimensions de la feuille |
| `context_limit_exceeded` | 400 | paramètres de contexte excessifs |
| `preview_timeout` | 504 | budget de lecture dépassé |

`chunk_stale` est le plus important : retomber silencieusement sur la première feuille
afficherait une source fausse avec l'apparence d'une source vraie.

---

## 9. Sécurité

- **Permission d'abord.** `load_accessible_document` — la même porte que le
  téléchargement — est appelée **avant** toute ouverture de fichier, toute lecture de
  cache, tout listage de feuilles, toute résolution de chunk. Aucune règle
  d'autorisation n'est réinventée.
- **Anti-énumération** conservée : inaccessible et inexistant rendent tous deux `404`.
- **IDOR par `chunk_id`** : appartenance au document **et** au RagSpace vérifiées
  (`Chunk.rag_space` est dupliqué exprès pour cela).
- **Cache** : la clé porte `document_id`, `sha256`, la fenêtre, la version de schéma et
  la version de normalisation — jamais l'utilisateur. La réponse ne dépend que du
  fichier, et la permission étant contrôlée en amont, le cache ne peut servir une
  réponse que l'appelant ne pourrait pas recalculer lui-même.
- **Contenu de cellule** : transporté comme texte. Aucune interprétation HTML côté
  serveur ; le rendu React échappe (aucun `dangerouslySetInnerHTML`). Les amorces de
  formule (`=`, `+`, `-`, `@`) sont neutralisées par la normalisation d'ingestion — sans
  quoi un copier-coller vers Excel exécuterait la cellule.
- **Journalisation** : identifiants, feuille, plages, durée, nombre de cellules,
  `reason_code`. **Jamais** une valeur de cellule, jamais le texte d'un chunk.

---

## 10. Ce que l'aperçu ne fait pas

Annoncé dans **chaque** réponse (`fidelity.omitted`), parce qu'un aperçu simplifié qui
ne le dit pas laisse croire à une fidélité qu'il n'a pas : styles, couleurs, largeurs
de colonnes, hauteurs de lignes, images, graphiques, tableaux croisés, mises en forme
conditionnelles, commentaires, volets figés, filtres, cellules fusionnées, mise en page
d'impression.

**Les pages d'impression Excel sont hors périmètre**, et pas seulement du MVP. Aucune
métadonnée de pagination (`print_area`, `pageSetup`, sauts de page) n'existe dans le
dépôt, openpyxl en `read_only` ne les expose pas de façon fiable, et la pagination
dépend de réglages (échelle, marges, pilote) qui peuvent placer la même plage sur des
pages différentes. Ancrer la vérification d'une citation sur une notion non
déterministe la rendrait fausse. **L'unité de navigation est la feuille et la plage.**

**Le CSV n'est pas pris en charge.** Il est bien ingéré, mais ses chunks passent par le
chunker texte générique : ni feuille, ni plage, ni numéro de ligne persisté. L'aperçu ne
saurait pas où surligner. Il rend `unsupported_format`, et le frontend affiche l'état
explicite avec téléchargement.

---

## Références d'implémentation

- `backend/apps/rag_core/services/preview/service.py` (orchestration, ordre imposé)
- `backend/apps/rag_core/services/preview/locator.py` (localisation, refus des conflits)
- `backend/apps/rag_core/services/preview/reader.py` (lecture bornée, normalisation)
- `backend/apps/rag_core/services/preview/{config,errors}.py` (bornes, codes stables)
- `backend/api/v1/documents/views_preview.py`, `backend/api/v1/urls.py`
- `backend/apps/rag_core/services/query/source_meta.py` (projection `data_ranges`)
- `backend/apps/rag_core/management/commands/bench_tabular_preview.py` (banc de mesure)
- Tests : `tests/tests_root/unit/preview/test_tabular_preview_fidelity.py` (oracle),
  `tests/tests_root/unit/preview/test_tabular_preview_bounds.py`,
  `tests/tests_root/http_api/test_documents_tabular_preview.py`,
  `tests/tests_root/integration/test_xlsx_source_to_preview.py` (bout-en-bout)
