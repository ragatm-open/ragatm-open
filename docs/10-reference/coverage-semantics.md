# Sémantique de couverture — que signifie « analysé » dans RagATM

Source de vérité du vocabulaire d'exhaustivité. Toute analyse portant sur un
corpus doit déclarer son périmètre selon ces définitions.

Implémentation : [`apps/rag_core/domain/coverage.py`](../../backend/apps/rag_core/domain/coverage.py)
(module pur) et [`apps/rag_core/services/coverage_service.py`](../../backend/apps/rag_core/services/coverage_service.py)
(agrégations SQL).

---

## 1. Pourquoi ce document existe

Une réponse RAG est construite à partir d'un nombre volontairement limité
d'extraits. Sans déclaration de périmètre, rien ne distingue cette réponse d'une
analyse complète du corpus. **Le risque n'est pas la lacune fonctionnelle : c'est
l'affirmation trompeuse.**

Ce document fixe donc ce qu'on a le droit de dire, et surtout ce qu'on n'a pas le
droit de dire.

---

## 2. Vocabulaire

### Corpus analysé

L'ensemble des documents qui :

- appartiennent au RagSpace demandé ;
- sont **visibles par l'utilisateur appelant** ;
- subsistent après application des filtres explicites ;
- quel que soit leur statut technique.

C'est le **dénominateur déclaré** de toute analyse. Il n'inclut jamais un
document auquel l'appelant n'a pas accès — le périmètre visible est résolu
**avant** le calcul de couverture.

### Document considéré

Document appartenant au corpus analysé et compté dans le dénominateur.

### Document analysé

Document dont le contenu exploitable a **réellement été parcouru** par le
traitement concerné.

> Un document sans contenu textuel exploitable **ne peut pas** être compté comme
> analysé, même si son statut technique est « prêt ». Il est exclu, avec sa
> raison.

### Document avec résultat

Document analysé ayant produit **au moins un élément pertinent** pour l'analyse
en cours. Notion **relative à la question posée**, jamais absolue.

---

## 3. Les quatre niveaux d'exhaustivité

| Niveau | Nom | Garanti ? | Mesurable ? |
|---|---|---|---|
| **A** | **Couverture documentaire** — tous les documents autorisés ont été considérés | ✅ **Oui** | ✅ Oui (`COUNT`) |
| **B** | **Couverture textuelle** — tous les contenus textuels exploitables des documents considérés ont été parcourus | ✅ **Oui** | ✅ Oui |
| **C** | **Couverture sémantique** — toutes les formulations pertinentes ont été reconnues | ❌ **Non** | ⚠️ Estimable seulement contre une vérité terrain |
| **D** | **Couverture métier** — toutes les notions métier ont été identifiées, normalisées et rapprochées | ❌ **Non** | ⚠️ Estimable seulement contre une vérité terrain |

**La ligne à ne jamais franchir : A et B sont des garanties ; C et D sont des
estimations.** Le code ne produit jamais C ni D — une analyse qui les
revendiquerait devrait le prouver contre un corpus de référence annoté.

---

## 4. Règles lexicales (contraignantes)

| Condition | Règle |
|---|---|
| `coverage_complete = false` | Le mot **« exhaustif » est interdit** dans le rendu utilisateur |
| `coverage_level = A` ou `B` | Couverture mesurable et vérifiable |
| `coverage_level = C` ou `D` | Couverture **probabiliste**, jamais garantie |
| `documents_excluded` non vide | La **raison et le nombre** doivent être affichés |
| `items_without_source > 0` | Le résultat analytique concerné est **rejeté ou marqué invalide** |

Le mot « exhaustif » ne doit **jamais** être employé comme synonyme de
« détaillé ».

**Même quand `coverage_complete = true`, le rendu ne promet pas l'exhaustivité.**
Une couverture complète aux niveaux A et B ne dit rien des niveaux C et D. La
formulation retenue est :

> Tous les documents et contenus textuels exploitables du périmètre ont été parcourus.

Sont proscrites : « analyse exhaustive », « intégralité du corpus analysée »,
« tous les éléments ont été trouvés », « aucune information n'a été manquée ».

---

## 5. Invariants

```
0 <= documents_analyzed     <= documents_considered <= documents_total
0 <= documents_with_results <= documents_analyzed
0.0 <= coverage_ratio <= 1.0

coverage_complete = true
  ⇒ documents_analyzed == documents_considered
  ⇒ documents_excluded est vide
  ⇒ limits_applied est vide
```

Violer un invariant lève `CoverageError`. Il n'y a pas de rattrapage silencieux :
une couverture fausse est pire qu'une absence de couverture.

### Convention pour un périmètre vide

`documents_considered == 0` ⇒ `coverage_ratio = 1.0` et `coverage_complete = true` :
il n'y avait rien à analyser, donc rien n'a été manqué.

**Mais le rendu utilisateur traite ce cas à part** et n'affiche jamais
« couverture 100 % » sur un espace vide — il affiche « Aucun document à analyser
dans ce périmètre ». Un espace vide n'est pas un espace exhaustivement analysé.

Les **ratios de statut** (`ready`, `failed`) valent en revanche `0.0` sur un
périmètre vide, pour éviter une division par zéro et un `1.0` trompeur.

---

## 6. Raisons d'exclusion

Ensemble **borné**. Un statut non reconnu tombe dans `unknown` : il est signalé,
jamais absorbé silencieusement dans le dénominateur.

| Code interne | Libellé utilisateur |
|---|---|
| `extraction_failed` | échec d’extraction |
| `not_ready` | document non encore prêt |
| `processing` | traitement en cours |
| `no_text_content` | aucun contenu textuel exploitable |
| `unsupported` | format non exploitable |
| `filtered_out` | exclu par un filtre |
| `limit_applied` | limite de traitement appliquée |
| `unknown` | état non reconnu |
| `permission_filtered` | **jamais sérialisé vers l'utilisateur** |

`permission_filtered` révélerait l'existence de documents inaccessibles. Le
périmètre visible étant résolu en amont, ce code ne devrait jamais apparaître ;
il existe pour que l'oubli soit détectable, pas divulgué.

Les libellés utilisateur sont **neutres** : jamais d'exception Python, de nom de
modèle, de provider, de port, de flag, de worker ni de nom de file.

---

## 7. Correspondance statut → couverture

| Statut du document | Considéré | Analysé | Exclusion |
|---|---|---|---|
| `READY` **avec** au moins un extrait | oui | **oui** | — |
| `READY` **sans** extrait | oui | non | `no_text_content` |
| `PENDING` | oui | non | `not_ready` |
| `INGESTING` | oui | non | `processing` |
| `FAILED` | oui | non | `extraction_failed` |
| *statut inconnu* | oui | non | `unknown` |

---

## 8. Comptage des pages

Il n'existe **pas de modèle `Page`**. Les pages sont dérivées des extraits :

```
pages_numbered_total = COUNT(DISTINCT (document_id, page_number))
                       WHERE page_number IS NOT NULL
```

Conventions explicites :

- **`page_number IS NULL` ne compte pas comme une page.** Compter un document non
  paginé comme « une page » serait une statistique inventée. Ces documents sont
  comptés séparément dans `documents_without_page_numbers`.
- **`page_number = 0` est un numéro valide** et compte normalement.
- Deux documents partageant le numéro 1 ont bien **deux** pages distinctes : la
  clé de comptage est le couple `(document, page)`, jamais le seul numéro.

---

## 9. Tokens estimés

```
estimated_tokens_total = ceil(characters_total / 4)
```

Estimation volontairement naïve : aucun tokenizer, aucune dépendance externe. Le
champ s'appelle **`estimated_tokens_total`**, jamais `tokens_total` — la valeur
est un ordre de grandeur, pas une mesure.

---

## 10. Empreinte de corpus

```
corpus_fingerprint = sha256( trié( f"{document_id}:{sha256}:{status}" ) )
```

**Change** si un document est ajouté, supprimé, si son contenu change ou si son
statut change. **Ne change pas** si l'ordre SQL change.

Ne contient **aucun** nom de fichier, **aucun** texte de document, **aucune**
donnée utilisateur en clair.

---

## 11. Ce que cette brique ne fait pas

Aucun appel LLM, aucun embedding, aucun retrieval, aucun reranking, aucune tâche
asynchrone, aucune dépendance nouvelle.

**Aucune logique métier.** Le calcul est rigoureusement identique quel que soit le
contenu documentaire — finance, technique, presse, juridique. Aucun mot-clé
métier n'intervient, et aucune couverture sémantique ou métier n'est inventée.
