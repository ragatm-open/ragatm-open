# Moteur tabulaire — opérations, typage et ambiguïtés

Objectif : recenser ce que le moteur tabulaire sait calculer, ce qu'il refuse
explicitement, comment il type les cellules et quand il demande une clarification.
Public : développeurs backend, intégrateurs. Le CODE fait foi.

> **Statut** : *implémenté*, **activable par flag** — `TABULAR_QUERY_ENABLED` vaut **`0`
> par défaut**. Flag à `0`, le refus historique `excel_guard` est inchangé.

Architecture : [tabular-query-architecture.md](tabular-query-architecture.md) ·
Sécurité : [tabular-query-security.md](tabular-query-security.md).

---

## 1. Opérations supportées

Liste **fermée** (`plan_schema.Operation`). Toute autre valeur est refusée.

| Opération | Colonne cible | Retourne | Notes |
|---|---|---|---|
| `filter_rows` | non | lignes | projection filtrée |
| `count_rows` | non | scalaire | `COUNT(*)` |
| `count_distinct` | **oui** | scalaire | `COUNT(DISTINCT c)` |
| `sum` | **oui** (numérique) | scalaire | `SUM(c)` |
| `average` | **oui** (numérique) | scalaire | somme et décompte exacts, division en `Decimal` côté Python |
| `minimum` | **oui** (ordonnable) | scalaire | `MIN(c)` |
| `maximum` | **oui** (ordonnable) | scalaire | `MAX(c)` |
| `top_n` | **oui** (ordonnable) | lignes | `limit` **exigée** et non nulle, `order_by` exigé |
| `bottom_n` | **oui** (ordonnable) | lignes | idem |
| `argmin` | **oui** (ordonnable) | ligne | ex æquo refusé |
| `argmax` | **oui** (ordonnable) | ligne | ex æquo refusé |

Cohérences imposées par le schéma :

- une opération qui exige une cible et n'en a pas est refusée — et une opération qui n'en
  prend pas mais en reçoit une l'est aussi ;
- `order_by` sur une opération scalaire est une **incohérence**, pas une préférence de
  présentation : `count_rows` ne trie rien ;
- une même colonne ne peut pas figurer deux fois dans `order_by` : l'ordre serait
  indéterminé.

### `average` : pourquoi pas `AVG()`

`AVG()` et l'opérateur `/` de DuckDB renvoient tous deux un `DOUBLE`. Un cast après coup
ne rattrape pas la précision déjà perdue. La requête rend donc `(SUM, COUNT)` et la
division est faite en `Decimal` côté Python. Le dénominateur est le **décompte des valeurs
non nulles** — les valeurs réellement agrégées, jamais le nombre de lignes brut.

### `argmin` / `argmax` : pourquoi deux lignes

La requête demande `LIMIT 2` exprès. Si les deux lignes portent la même mesure, « quelle
ligne » n'a **pas de réponse unique** : le moteur lève `tie_unresolved` plutôt que de
trancher. Un `LIMIT 1` masquerait l'égalité et choisirait arbitrairement — précisément ce
que le moteur refuse. Les valeurs nulles sont écartées : un `NULL` trié en tête donnerait
un « argmin » qui ne mesure rien.

---

## 2. Opérations différées — refus explicite

Reconnues et **nommées**, pour être refusées avec un message utile (« hors périmètre »)
plutôt qu'un « opération inconnue ». Code : `unsupported_operation`.

| Famille | Opérations différées |
|---|---|
| Statistiques | `median`, `percentile`, `stddev`, `variance` |
| Regroupement | `group_by` |
| Multi-source | `join`, `multi_file`, `multi_table` |
| Analytique | `window`, `pivot`, `regression`, `anomaly_detection` |

`PlanSource` n'accepte qu'un **triplet unique** `(document_id, sheet_id, table_id)` : le
lot interdit structurellement toute jointure ou source multiple.

Ce refus est un choix, pas un manque : les nommer garantit qu'elles ne seront jamais
traitées par approximation.

---

## 3. Opérateurs de filtre

Liste fermée (`plan_schema.FilterOperator`). Chaque opérateur a un gabarit SQL
**constant** ; seuls les `?` reçoivent des valeurs.

| Opérateur | Arité | Types acceptés | SQL |
|---|---|---|---|
| `equals` | `value` | tous | `c = ?` — sur colonne **texte** : `lower(trim(c)) = lower(trim(?))` |
| `not_equals` | `value` | tous | `c IS DISTINCT FROM ?` — sur colonne **texte** : `lower(trim(c)) IS DISTINCT FROM lower(trim(?))` |
| `greater_than` | `value` | ordonnables | `c > ?` |
| `greater_or_equal` | `value` | ordonnables | `c >= ?` |
| `less_than` | `value` | ordonnables | `c < ?` |
| `less_or_equal` | `value` | ordonnables | `c <= ?` |
| `between` | `values` (exactement 2, non nulles) | ordonnables | `c BETWEEN ? AND ?` |
| `in` | `values` (liste non vide) | tous | `c IN (?, ?, …)` |
| `contains` | `value` | texte uniquement | `c IS NOT NULL AND strpos(lower(c), lower(?)) > 0` |
| `starts_with` | `value` | texte uniquement | `c IS NOT NULL AND starts_with(lower(c), lower(?))` |
| `is_null` | aucune | tous | `c IS NULL` |
| `is_not_null` | aucune | tous | `c IS NOT NULL` |

Contrôles appliqués :

- **cohérence arité/valeur** (schéma) : un opérateur unaire refuse toute valeur, `in`
  exige `values` non vide, `between` exige exactement deux bornes non nulles, les autres
  exigent `value` et refusent `values` ;
- **compatibilité de type** (validateur sémantique) : `contains`/`starts_with` sur une
  colonne numérique ou date → `incompatible_filter_type` ; un opérateur d'ordre sur une
  colonne non ordonnable → idem ;
- **convertibilité des bornes** : chaque valeur de filtre doit être convertible vers le
  type de la colonne. Une borne non convertible est une erreur de conception du plan, pas
  une ligne à ignorer.

Pour `in`, le nombre de `?` est généré depuis le **nombre** de valeurs : la seule chose
que les valeurs déterminent est la quantité de marqueurs.

### Égalité sur colonne texte

Sur une colonne `text` ou `identifier`, `equals` et `not_equals` comparent en
**minuscules et sans espaces de bordure**. « ouest », « Ouest » et « Ouest  » désignent
la même modalité pour un utilisateur ; exiger la casse exacte produirait un résultat vide
présenté comme un fait — précisément le mode d'échec que le moteur cherche à éviter.

Le choix du gabarit dépend du **type inféré de la colonne**, transmis au compilateur par
le service ; les gabarits restent des constantes du module et les valeurs restent des
paramètres.

---

## 4. Système de types

Trois règles gouvernent `tabular/types.py` :

1. **Un identifiant n'est jamais un nombre.**
2. **Jamais de `float` pour une valeur métier** — `Decimal` de bout en bout.
3. **Une valeur invalide n'est jamais convertie en zéro.**

### Types du catalogue

| Type | Sens | Type DuckDB |
|---|---|---|
| `text` | texte libre | `VARCHAR` |
| `identifier` | référence, code, matricule | `VARCHAR` |
| `boolean` | booléen | `BOOLEAN` |
| `integer` | entier | `BIGINT` |
| `decimal` | décimal | `DECIMAL(38, 10)` |
| `percentage` | pourcentage | `DECIMAL(38, 10)` |
| `currency` | montant avec devise | `DECIMAL(38, 10)` |
| `date` | date | `DATE` |
| `datetime` | horodatage | `TIMESTAMP` |

| Famille | Composition | Usage |
|---|---|---|
| `NUMERIC_TYPES` | `integer`, `decimal`, `percentage`, `currency` | `sum`, `average` |
| `ORDERED_TYPES` | numériques + `date`, `datetime` | `min`, `max`, `argmin`, `argmax`, tri, comparaisons |
| `TEXT_TYPES` | `text`, `identifier` | `contains`, `starts_with` |

`DECIMAL(38, 10)` couvre les montants comptables sans jamais passer par un flottant. Les
identifiants restent du **texte** : c'est ce qui préserve `001245`.

### Identifiants à zéro initial

`looks_like_identifier()` reconnaît deux formes : une suite de chiffres de longueur > 1
commençant par `0`, ou un code alphanumérique mêlant lettres et chiffres (2 à 64
caractères, `A-Za-z0-9._-/`).

Le test d'identifiant est fait **avant** le test numérique : `001245` est numériquement
convertible, et c'est précisément le piège. Le zéro initial n'est pas une décoration — il
fait partie de la valeur, et le perdre casse silencieusement toute recherche ou
rapprochement ultérieur.

### Nombres — conventions française et anglaise

`parse_decimal()` gère les deux conventions en retenant le **dernier** séparateur comme
séparateur décimal :

| Entrée | Résultat | Raison |
|---|---|---|
| `1 234,56` | `1234.56` | espaces (dont insécables) retirés, virgule décimale |
| `1,234.56` | `1234.56` | dernier séparateur = `.` |
| `(1 234,56)` | `-1234.56` | convention comptable : parenthèses = négatif |
| `1234,56-` | `-1234.56` | signe suffixé |
| `12,5 %` | `0.125` | Excel stocke un pourcentage comme fraction ; l'affichage n'est pas la valeur |
| `1 234,56 €` | `1234.56`, unité `EUR` | symbole retiré, devise conservée |
| **`1,234`** | **refus** `mixed_column_type` | ambigu : 1.234 ou 1234 selon la locale |

Le refus de `1,234` (séparateur unique suivi d'exactement trois chiffres) est délibéré :
deviner produirait un faux résultat silencieux. Le paramètre interne `allow_ambiguous`
existe mais n'est pas ouvert par le chemin de production.

### Devises

Symboles reconnus → code ISO : `€`/`EUR` → `EUR`, `$`/`USD` → `USD`, `£`/`GBP` → `GBP`,
`¥`/`JPY` → `JPY`, `CHF` → `CHF`. La devise est détectée depuis la valeur **ou** le format
de nombre, et **conservée** dans `CatalogColumn.unit` : un montant sans devise est un
nombre sans signification métier. Elle est reportée dans `result_unit` de la preuve.

Une colonne `currency` sans devise déterminée reste calculable (le calcul est exact) ;
l'unité n'est simplement pas inventée.

### Dates

| Entrée | Résultat |
|---|---|
| objet `date` / `datetime` (natif Excel) | converti directement |
| `2025-04-03` | `date(2025, 4, 3)` |
| `25/12/2025` | `date(2025, 12, 25)` — le jour > 12 lève l'ambiguïté |
| **`03/04/2025`** | **refus** `mixed_column_type` : jour/mois indiscernables sans locale explicite |
| tout autre format | refus `mixed_column_type` |

### Booléens

`true`/`vrai`/`1`/`oui`/`yes` et `false`/`faux`/`0`/`non`/`no`. Toute autre valeur est
refusée.

### Cellules vides, erreurs Excel, types mixtes

- **Vide** = `None` ou chaîne blanche. `0` et `False` **ne sont pas vides** — ce sont des
  valeurs. Une cellule vide devient `NULL`.
- **Erreurs Excel** (`#REF!`, `#DIV/0!`, `#VALUE!`, `#NAME?`, `#NULL!`, `#NUM!`, `#N/A`,
  `#GETTING_DATA`) lèvent `excel_error_value`. Elles ne sont **jamais** traitées comme du
  texte.
- **Type mixte** : un mélange entier/décimal est unifié en `decimal`. Tout autre mélange
  rend la colonne `text`, ce qui **interdira** d'y sommer quoi que ce soit — plutôt qu'une
  conversion partielle qui laisserait tomber des lignes en silence.
- Une valeur décimale dans une colonne inférée `integer` lève `mixed_column_type`.

### Sortie numérique

Les nombres sortent en **chaîne décimale** (`"1284350.00"`), pas en flottant JSON.
L'arrondi, quand une échelle est demandée, est `ROUND_HALF_UP` — convention comptable :
Python arrondit `HALF_EVEN` par défaut, ce qui transforme `2.5` en `2` et surprend sur une
facture.

---

## 5. Ambiguïtés et clarification

Le mode d'échec visé est le même partout : **une réponse numériquement exacte, calculée
sur la mauvaise chose**. Elle est indétectable pour l'utilisateur, puisque le chiffre a
l'air juste.

### Sélection de table

`table_selector.select_table` note chaque table candidate :

| Signal | Poids |
|---|---|
| feuille explicitement nommée dans la question | `+3.0` |
| couverture des colonnes citées | `+2.0 × taux de couverture` |
| termes de la question présents dans les en-têtes | `+0.2 × nombre de termes` |
| feuille d'archive ou de projection | `−2.5` |

Le motif de pénalité couvre `archive(s)`, `historique`, `history`, `old`, `ancien`,
`previsionnel`, `prevision`, `forecast`, `budget_prev`, `simulation`, `scenario` : ces
feuilles répondraient sur des données historiques ou simulées, elles ne sont **jamais** un
défaut implicite.

Si le meilleur score est ≤ 0 ou si l'écart avec le second est < `1.0` :
`ambiguous_table` — jamais la première table, jamais la plus grande, jamais celle de la
première feuille. Une table unique est retenue sans scoring.

### Résolution de colonne

Priorité **stricte et vérifiable**, chaque niveau ne s'exécutant que si le précédent n'a
rien donné d'unique :

| Niveau | Nom | Score |
|---|---|---|
| 1 | `exact_header` — en-tête identique | `1.00` |
| 2 | `normalized_header` — en-tête normalisé | `0.95` |
| 3 | `business_alias` — alias métier | `0.90` |
| 4 | `lexical` — tous les termes demandés présents | `0.80 − 0.02 × mots en trop` |
| 5 | `fuzzy` — proximité RapidFuzz | `≤ 0.70` (plancher de ratio `75`) |

L'écart entre `exact` et `fuzzy` est délibérément large : aucune combinaison de
correspondances floues ne doit rattraper une correspondance exacte. RapidFuzz est un
**signal secondaire** — il peut départager, jamais élire contre une correspondance
structurelle. Son absence dégrade la résolution (repli `difflib`) sans la casser.

Le niveau `lexical` décroît avec les mots **en trop** côté colonne : « montant net » élit
« Montant net » plutôt que « Montant net payé ».

Trois refus :

| Cas | Code |
|---|---|
| aucun candidat au-dessus de `TABULAR_COLUMN_MATCH_MIN_SCORE` (défaut `0.60`) | `column_not_found` |
| deux colonnes de même nom normalisé | `duplicate_column_name` |
| écart au second < `TABULAR_COLUMN_MATCH_MIN_MARGIN` (défaut `0.15`) | `ambiguous_column` |

Le cas canonique de dominance insuffisante : « Montant net comptable » contre « Montant net
payé ». Trancher produirait une réponse exacte sur la mauvaise colonne.

`required_types` filtre les candidates **avant** le classement : proposer une colonne
texte comme candidate à une somme n'aiderait personne à trancher
(`incompatible_column_type` si aucune candidate compatible).

Alias métier — volontairement **conservateurs**, un alias erroné produisant exactement le
défaut qu'on cherche à éviter : `ca` ↔ `chiffre_d_affaires`/`chiffre_affaires`,
`montant`/`somme`, `quantite`/`qte`/`nombre`, `prix_unitaire`/`pu`, `date`,
`reference`/`ref`, `region`.

### Sélection de document

Plusieurs documents tabulaires accessibles → le nom de fichier cité dans la question
départage ; sinon `ambiguous_document`.

### Planner déterministe

`tabular/planner.py` traduit la question en plan par **règles et motifs**, sans aucun LLM.
Le parti pris est assumé : pour ce lot, la fiabilité prime sur la couverture linguistique.
Un planner qui comprend une partie des formulations et refuse proprement le reste vaut
mieux qu'un planner qui comprend davantage et se trompe silencieusement.

Quand aucune règle ne s'applique : `planner_could_not_build_plan`
(statut `unsupported`). La question repart vers le refus explicite existant, **jamais**
vers le RAG vectoriel.

Détails notables :

- `argmin`/`argmax` sont distingués de `minimum`/`maximum` par le « **quel** » : la
  question porte sur l'identité d'une ligne, pas sur une valeur.
- un filtre dont la colonne n'est pas identifiable **fait échouer** le plan : l'abandonner
  en silence donnerait une somme sur toute la table présentée comme une somme filtrée ;
- un seuil numérique (« supérieur à 1000 ») n'est appliqué que s'il existe **exactement
  une** colonne numérique : sinon, à quel seuil se rapporterait le nombre ?
- la colonne d'identité rendue par `argmin`/`argmax` est le premier `identifier` par ordre
  lexicographique, sinon le premier `text` par position — arbitraire dans son fond, mais
  **stable** entre exécutions et entre machines ;
- `TABULAR_LLM_PLANNER_ENABLED` est **non branché** (défaut `0`). Un planner LLM
  produirait le **même** `TabularPlan` et passerait par les **mêmes** validations : aucun
  privilège.

---

### 5.4 Rattachement d'un seuil numérique à sa colonne (R1)

Statut : *implémenté et testé*.

Un seuil (« supérieur à 1 000 ») doit désigner **une** colonne. Le signal décisif est
positionnel : le nom de la mesure **précède** l'opérateur.

| Question | Colonne du filtre | Signal |
|---|---|---|
| « somme du **CA HT** supérieur à 1 000 » | `CA HT` | nom explicite avant l'opérateur |
| « dont la **quantité** dépasse 100 » | `Quantité` | idem |
| « total des ventes dont le **prix unitaire** dépasse 50 » | `Prix unitaire` | filtre ≠ mesure agrégée |
| « somme du montant supérieur à 1 000 » (1 seule colonne numérique) | `Montant` | rattachement forcé |
| « total des ventes supérieures à 1 000 » | la mesure agrégée | aucune autre colonne nommée |

Ordre de résolution, du plus fort au plus faible :

1. le groupe nominal qui précède l'opérateur nomme une colonne ;
2. il n'existe qu'**une** colonne numérique ;
3. la mesure agrégée est numérique et rien d'autre n'est nommé ;
4. sinon → `ambiguous_filter_column`.

**Ce qui continue de refuser.** « total des **lignes** supérieures à 1 000 » ne nomme
aucune mesure. Les mots de *population* (`lignes`, `enregistrements`, `éléments`,
`valeurs`, `résultats`…) sont reconnus comme tels et **n'entrent pas** dans la
résolution — sans cette exclusion, le score lexical finissait par accrocher une
colonne au hasard.

Le code `ambiguous_filter_column` est **distinct** d'`ambiguous_column` : ce n'est pas
la mesure qui est ambiguë, c'est la cible du filtre. Deux colonnes voisines
(« Montant net comptable » / « Montant net payé ») ne sont jamais arbitrées.

Un pourcentage est normalisé en fraction (`12 %` → `0.12`) sur une colonne de type
`percentage` — comme Excel. Filtrer sur `12` ne retiendrait rien et rendrait un
résultat vide présenté comme un fait. Les bornes d'un `between` inversé sont remises
dans l'ordre.

### 5.5 Dates ISO dans un CSV (R2)

Statut : *implémenté et testé*.

Dans un CSV, tout est du texte : une colonne de dates ISO arrivait donc en `text`, ce
qui interdisait `minimum`, `maximum`, un tri chronologique et un `between` de période.

**Formats reconnus** (strictement) :

```
YYYY-MM-DD
YYYY-MM-DD HH:MM:SS
YYYY-MM-DDTHH:MM:SS
YYYY-MM-DDTHH:MM:SSZ
YYYY-MM-DDTHH:MM:SS±HH:MM
```

**Jamais convertis en date** — et c'est le risque central : beaucoup d'identifiants
métier ressemblent à des dates.

| Valeur | Pourquoi refusée |
|---|---|
| `REF-2026-04`, `Series-2025-01` | référence métier ; l'accepter produirait un tri silencieusement faux |
| `2026-13-01` | mois inexistant — **aucune expression régulière ne le détecte**, la validation passe par le calendrier |
| `2026-02-31` | jour inexistant dans ce mois |
| `0012-AB-34` | forme voisine, pas une date |
| `2026-1-5`, `20260115` | pas de l'ISO strict |
| `15/01/2026` | format local, ambigu sans locale |

**Politique d'inférence**, explicite et testée :

| Constante | Valeur | Raison |
|---|---|---|
| `MIN_DATE_SAMPLE` | `3` | sous ce seuil il n'y a pas de preuve : deux cellules qui ressemblent à des dates peuvent être un accident |
| `DATE_RATIO_REQUIRED` | `1.0` | une seule intruse obligerait à décider quoi en faire — et toute réponse (ignorer, mettre à `NULL`) fausse un `minimum` sans que rien ne le signale |

Une colonne non homogène reste `text`, ce qui fait **refuser** les opérations
temporelles avec un code explicite plutôt que de produire un résultat amputé. Une
valeur non conforme rencontrée **après** l'échantillon (grand fichier) fait échouer la
requête avec un code — **jamais** un `NULL` muet.

**Fuseaux horaires** : normalisés en **UTC**. `minimum`/`maximum` ordonnent donc des
*instants*. Sans normalisation, `08:30+02:00` et `08:30Z` se compareraient comme des
chaînes et l'ordre serait faux de deux heures. Le fait qu'une valeur portait un fuseau
est détectable via `types.has_timezone`.

## 6. Formules Excel

Distinction exigée par la spécification, et qui doit rester lisible dans la réponse :
**lire une valeur en cache n'est pas recalculer un classeur**.

| Situation | Comportement |
|---|---|
| Formule **avec** valeur en cache | la valeur en cache est lue et utilisée ; un avertissement le signale |
| Formule **sans** valeur en cache, opération de calcul (`sum`, `average`, `minimum`, `maximum`, `argmin`, `argmax`) | **refus** `formula_result_unavailable` |

Une formule non recalculée vaut `None` côté openpyxl : indiscernable d'une cellule vide.
La sommer produirait un total silencieusement faux. **RagATM ne recalcule jamais un
classeur** — il refuse.

La détection repose sur la **double lecture** du classeur (`data_only=True` pour les
valeurs, `data_only=False` pour repérer les cellules porteuses de formule) et l'état est
porté **au niveau de la table**, pas de la feuille : une table sans formule dans une
feuille qui en contient ne doit pas être marquée à tort.

Avertissement systématique quand la table porte des formules :
> valeurs de formules lues depuis le cache du classeur ; RagATM ne recalcule jamais un
> classeur

---

## 7. Provenance et preuve

Champs de `provenance.Evidence`, tous dérivés de l'**exécution réelle** :

| Champ | Contenu |
|---|---|
| `document_id`, `filename` | document interrogé |
| `content_sha256` | hash du contenu — lie la réponse à une version précise du fichier |
| `sheet_name`, `table_id` | feuille et table effectivement utilisées |
| `data_range`, `header_range` | plages Excel — jamais approximées |
| `operation` | opération exécutée |
| `target_column`, `target_column_id` | colonne mesurée, par nom **et** par identifiant |
| `result_unit` | devise ou unité de la colonne cible |
| `filters` | filtres appliqués, colonne par **nom d'origine**, opérateur, valeurs canoniques |
| `selected_row_count` | lignes retenues par les filtres — **recompté**, jamais déduit |
| `table_row_count` | lignes de la table |
| `plan_hash` | SHA-256 du plan canonique |
| `evidence_hash` | SHA-256 de `plan_hash \| content_sha256 \| table_id \| data_range \| selected_row_count \| résultat` |
| `row_references` | lignes précises, quand le résultat en désigne |
| `formulas` | `table_has_formulas`, `uncached_formula_present`, `recalculated_by_ragatm` (toujours `false`) |
| `execution_id` | identifiant de la trace `TabularQueryExecution` |

Les deux hashes permettent de **rejouer et comparer** une réponse sans stocker les valeurs
sensibles du tableau.

Une réponse numériquement correcte accompagnée d'une provenance fausse est comptée comme
un **échec**. La narration LLM éventuelle (`TABULAR_NARRATION_ENABLED`, **non branché**,
défaut `0`) décrit le résultat structuré : elle ne le produit pas, ne le corrige pas, ne
l'arrondit pas.

---

## 8. Codes d'erreur

Codes **stables** : exposables en API, testables, journalisables, traduisibles côté front.
Un code ne change jamais de sens ; on en ajoute, on n'en recycle pas.

| Famille | Codes |
|---|---|
| Plan — structure | `invalid_plan_schema`, `invalid_plan_schema_version`, `unsupported_operation`, `unsupported_filter_operator`, `invalid_filter`, `invalid_limit`, `too_many_filters`, `multiple_source_tables`, `expression_forbidden` |
| Plan — sémantique | `document_not_found`, `sheet_not_found`, `table_not_found`, `column_not_found`, `column_not_in_table`, `table_not_in_sheet`, `sheet_not_in_document`, `incompatible_column_type`, `incompatible_filter_type`, `incompatible_units`, `catalog_stale`, `catalog_missing` |
| Ambiguïté | `ambiguous_column`, `ambiguous_table`, `ambiguous_sheet`, `ambiguous_document`, `duplicate_column_name` |
| Données | `formula_result_unavailable`, `excel_error_value`, `mixed_column_type`, `empty_selection`, `tie_unresolved`, `non_numeric_target_column` |
| Ressources | `too_many_rows_scanned`, `too_many_columns`, `too_many_result_rows`, `too_many_groups`, `query_timeout`, `memory_exceeded`, `too_many_candidate_tables`, `too_many_candidate_columns` |
| Périmètre | `tabular_query_disabled`, `permission_denied`, `no_tabular_intent`, `planner_could_not_build_plan`, `unsupported_file_format` |

---

## Références d'implémentation

- `backend/apps/rag_core/tabular/plan_schema.py` (opérations, opérateurs, cohérences)
- `backend/apps/rag_core/tabular/types.py` (typage, `Decimal`, identifiants, dates, devises)
- `backend/apps/rag_core/tabular/plan_compiler.py` (gabarits SQL, `average`, `argmin`/`argmax`)
- `backend/apps/rag_core/tabular/plan_validator.py` (compatibilités, formules sans cache)
- `backend/apps/rag_core/tabular/column_resolver.py` (niveaux, dominance, alias)
- `backend/apps/rag_core/tabular/table_selector.py` (scoring de table)
- `backend/apps/rag_core/tabular/planner.py` (planner déterministe)
- `backend/apps/rag_core/tabular/provenance.py` (preuve, hashes)
- `backend/apps/rag_core/tabular/result_formatter.py` (sortie, `ROUND_HALF_UP`)
- `backend/apps/rag_core/tabular/errors.py` (codes stables)
- Tests : `tests/tests_root/rag_core/test_tabular_engine.py`, `tests/tests_root/rag_core/test_tabular_integration.py`

---

## 9. Benchmark du moteur — statut *benchmarké*

Le moteur dispose d'un banc de mesure dédié, indépendant du moteur lui-même.

| Module | Rôle |
|---|---|
| `backend/apps/rag_core/benchmark/tabular_query_dataset.py` | corpus synthétique reproductible (graine fixe) + cas JSONL, **vérité terrain calculée en Python nu** |
| `backend/apps/rag_core/benchmark/tabular_query_runner.py` | exécution des cas sur la chaîne réelle (catalogue → planner → plan → compilation → DuckDB), 5 passes par cas |
| `backend/apps/rag_core/benchmark/tabular_query_metrics.py` | métriques de chemin, de valeur, de refus et de déterminisme |
| `tests/tests_root/rag_core/test_tabular_query_benchmark.py` | reproductibilité, exécution, métriques, règle d'indépendance |

**Règle d'indépendance (impérative).** Aucun de ces modules n'importe
`benchmark/tabular_reference_executor.py`, et le dataset n'importe rien de
`apps/rag_core/tabular/`. Une vérité terrain qui partagerait le normaliseur du moteur
reproduirait ses défauts de conversion des deux côtés, et ne les verrait jamais. La
règle est vérifiée par analyse d'AST dans les tests.

**Métriques produites** : `document_accuracy`, `sheet_accuracy`, `table_accuracy`,
`column_accuracy`, `operation_accuracy`, `filter_accuracy`, `plan_exact_match`,
`numeric_accuracy` (comparaison `Decimal` avec tolérance déclarée par le cas, jamais
d'égalité flottante), `clarification_precision` / `clarification_recall`,
`false_answer_rate`, `unsafe_query_rejection_rate`, `determinism_rate`, latences
p50 / p95.

Les cas couvrent quatorze catégories, dont des **cas négatifs** dont la bonne issue est
un refus (`clarification_required`, `unsupported`, `error`) avec un code attendu : un
refus pour la mauvaise raison est compté comme un échec.
