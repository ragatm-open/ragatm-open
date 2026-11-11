# Bruit structurel du contexte — questions et passages-titres

**Statut : implémenté, désactivé par défaut** (`RAG_STRUCTURAL_NOISE_PENALTY_ENABLED=0`).
Public : développeurs backend, exploitants.

> **Cette pénalité ne classe pas les anomalies, insuffisances ou constats. Elle réduit
> uniquement le poids de passages structurellement peu affirmatifs.**

---

## 1. Pourquoi

Des passages qui n'énoncent aucun fait — en-têtes de diapositives, titres de section,
listes de questions — occupent des créneaux de contexte. Deux mécanismes les y amènent :

| Constat mesuré | Valeur |
|---|---|
| Rang moyen dans le pool lexical | **questions 11,1** contre **constats 13,8** |
| Part de titres dans le pool vectoriel (questions de constats) | **26 %** |
| En-têtes (`title_like`) dans le corpus | 0,6 % (CM-2026) · 1,6 % (Mission) |
| En-têtes dans le top-8, jeu de 55 questions | **6 créneaux sur 431** |

Les questions du corpus se classent **mieux que les réponses** parce qu'elles reprennent
les mots de la requête ; les titres remontent surtout par le leg vectoriel.

> **Correction d'une mesure antérieure.** Une évaluation préalable avançait « 19 % du
> top-8 » : ce chiffre venait d'un classifieur d'analyse volontairement large (tout chunk
> de moins de 25 mots y comptait comme un titre, tout chunk court contenant un « ? »
> comme une question). Avec la définition **conservatrice** retenue ici, la mesure réelle
> est de **1,4 %** des créneaux. Le gain de ce mécanisme est donc réel mais **modeste** —
> il ne faut pas lui prêter l'ampleur annoncée par cette première estimation.

---

## 2. Ce que le mécanisme détecte

### Passage interrogatif (`question`)

Déclenche quand l'interrogatif **domine** : au moins **60 %** des unités du passage se
terminent par un point d'interrogation **et** il reste au plus **25 mots** de contenu
affirmatif.

Détecté : question isolée · liste de questions · demande de documents formulée en
questions · plusieurs phrases toutes interrogatives (FR et EN).

**Non détecté** (reste `normal`) : une réponse substantielle contenant une question
rhétorique ou citée · une question suivie de sa réponse dans le même chunk · un « ? »
interne à une URL ou à une chaîne technique · une liste majoritairement affirmative.

### Passage assimilable à un titre (`title_like`)

Exige **plusieurs signaux convergents** — la brièveté seule ne pénalise **jamais** :

1. passage court (≤ 25 mots) ;
2. **et** absence de ponctuation de fin de phrase (donc aucune proposition affirmative) ;
3. **et** un marqueur typographique : numérotation de section (`3.2 …`, `IV. …`),
   libellé de structure (`Annexe`, `Sommaire`, `Synthèse`, `Tableau`, `Chapitre`…),
   ou capitales dominantes.

Le nombre de **lignes** n'en fait délibérément pas partie : les en-têtes réels du corpus
tiennent sur plusieurs lignes (`1 POINT MARCHÉ` puis la date, un titre de diapositive
suivi de « Extrait »). L'exiger sur une seule ligne rendait le mécanisme **totalement
inerte** — zéro déclenchement sur 8 000 chunks.

Restent `normal` : « Le contrôle est réalisé mensuellement. » · « La validation de second
niveau est absente. » · « Aucun écart relevé » · « 1 234,56 EUR » · une ligne de tableau ·
un fragment de tableau multi-ligne sans marqueur typographique.

> `heading` et `section` **ne sont pas disponibles** dans les métadonnées actuelles
> (0 chunk sur 27 086 en dev). `title_like` est une **heuristique structurelle**, pas une
> métadonnée documentaire — d'où son nom et l'exigence de signaux convergents.

### Doctrine

**En cas de doute, le score d'origine est conservé.** Un passage ambigu ou mixte n'est
jamais pénalisé, et **aucun candidat n'est jamais supprimé**.

---

## 3. Position dans le pipeline

```
FTS + vectoriel → fusion RRF → seuils (should_answer) → PÉNALITÉ STRUCTURELLE
                → diversification (inchangée) → troncature top-k (inchangée)
```

Point d'application **unique**, dans `orchestrator.query_rag`. Conséquences voulues :

- la pénalité agit **uniquement sur `fusion_score`**, la clé de classement de la
  diversification. Le **`score` brut est conservé** — donc les seuils (`should_answer`)
  et la confiance faible (`low_confidence`) sont inchangés **par construction** ;
- `len(chunks)` est conservé : la sélection change, jamais la population ;
- l'**inventaire** et le **leg temporel** portent un ordre documentaire délibéré : la
  pénalité est sautée dans ces deux cas ;
- RRF, poids lexical/vectoriel, `candidate_k`, `top_k`, `max_per_group`, reranking et
  prompts sont **inchangés**.

Un facteur de 1,00 ne modifie rien, pas même l'ordre.

---

## 4. Configuration

| Variable | Défaut | Rôle |
|---|---|---|
| `RAG_STRUCTURAL_NOISE_PENALTY_ENABLED` | `0` | activation (doctrine du dépôt pour les mécanismes de classement) |
| `RAG_STRUCTURAL_NOISE_PENALTY_FACTOR` | `0.75` | facteur, borné à **[0,50 ; 1,00]** |

Un facteur hors bornes ou illisible est **bloquant au démarrage** (`rag_core.E006`) ;
un mécanisme actif avec un facteur de 1,00 est signalé comme inerte (`rag_core.W002`).
Une valeur illisible retombe sur **1,00 (neutre)** à l'exécution : une configuration
cassée ne doit jamais dégrader silencieusement un classement.

Un passage à la fois interrogatif et assimilable à un titre reçoit **un seul facteur**
(0,75), jamais 0,75 × 0,75.

---

## 5. Observabilité

Sur chaque candidat pénalisé : `passage_shape`, `structural_penalty_applied`,
`structural_penalty_factor`, `structural_penalty_reasons`, `fusion_score_before_penalty`.

Agrégats renvoyés par `query_rag` sous `structural_noise` et journalisés
(`retrieval.structural_noise`) : `candidates`, `question_detected`,
`title_like_detected`, `candidates_penalized`, `top_k_noise_before`,
`top_k_noise_after`, `factor`.

**Aucun texte documentaire, aucun nom de client, aucun chemin, aucun prompt** n'entre
dans les logs ou les agrégats — uniquement des compteurs. Ces informations ne sont pas
exposées à l'utilisateur final.

Raisons stables, testables : `single_question`, `majority_interrogative_sentences`,
`interrogative_list`, `interrogative_opener`, `numbered_title_like`,
`structural_label`, `uppercase_heading`, `short_nominal_heading`.

---

## 6. Activation et rollback

1. déployer avec le mécanisme désactivé (état livré) ;
2. exécuter le benchmark de référence ;
3. activer `ENABLED=1` avec `FACTOR=0.75` en staging ;
4. rejouer le benchmark, inspecter un échantillon, tester la génération réelle ;
5. décider du GO production.

**Rollback immédiat, sans réindexation** : `RAG_STRUCTURAL_NOISE_PENALTY_ENABLED=0`
(ou `FACTOR=1.00`).

---

## 7. Effet mesuré (dev, 55 questions, 2 corpus)

| Métrique | Désactivé | Facteur 0,75 | Écart |
|---|---:|---:|---:|
| Passages-titres dans le top-8 | 6 | **0** | **−100 %** |
| Passages interrogatifs dans le top-8 | 0 | 0 | — |
| Créneaux de contexte | 431 | 428 | −0,7 % |
| **Caractères de contenu transmis** | 495 689 | **504 022** | **+1,7 %** |
| Contenu moyen par créneau | 1 150 car. | 1 178 car. | +2,4 % |
| Recall des faits attendus | 3 % | 3 % | inchangé |
| Politiques (`ok`/`explain`/`low_confidence`) | 43/7/5 | 43/7/5 | identiques |
| Latence p50 · p95 | 777 · 3 502 ms | 835 · 3 708 ms | +58 · +206 ms |

**Le nombre de créneaux peut baisser sans perte d'information.** Exemple mesuré : une
question factuelle recevait 7 sources, dont **4 en-têtes de 36 caractères** ; après
pénalité elle en reçoit 3, pour un volume de texte quasi identique (11 050 contre
11 194 caractères). Les créneaux libérés ne se remplissent pas toujours : le passage
suivant peut ne plus tenir dans le budget de prompt (`RAG_MAX_PROMPT_CHARS`). Globalement
le contexte transporte **plus** de contenu, pas moins.

**Question interrogatives : aucun déclenchement mesuré.** À la granularité de chunking
actuelle (287 mots de médiane), un passage exclusivement interrogatif n'existe
pratiquement pas : sur 400 chunks contenant un « ? », le ratio interrogatif moyen est de
**3,5 %** et 4 seulement atteignent le seuil de 60 %. Le volet « question » du mécanisme
est donc correct mais **inerte sur ce corpus** — il ne le resterait pas avec un chunking
plus fin ou un corpus de type FAQ.

---

## 8. Limites

- Heuristique **structurelle** : elle ne comprend pas le sens, elle observe la forme.
- Un passage-titre qui porterait à lui seul l'information est dévalorisé, non exclu — il
  reste sélectionnable si aucun passage affirmatif ne le concurrence.
- Elle **ne traite pas** le problème de fond des constats négatifs : le rappel des
  anomalies documentées reste limité par la représentation de la requête, pas par le
  classement (cf. le rapport d'évaluation retrieval).
- **Aucune logique orientée findings** n'y figure : ni vocabulaire d'anomalie, ni
  d'insuffisance, ni terme négatif, ni classifieur métier. Le mécanisme ne cherche
  jamais à déterminer si un passage est un constat.

---

## Références d'implémentation

- `backend/apps/rag_core/services/query/passage_shape.py`
- `backend/apps/rag_core/services/query/orchestrator.py` (point d'application unique)
- `backend/apps/rag_core/checks_structural_noise.py`
- `backend/config/settings/base.py`
- `tests/tests_root/unit/query/test_passage_shape.py`
- `tests/tests_root/integration/test_structural_noise_penalty.py`
