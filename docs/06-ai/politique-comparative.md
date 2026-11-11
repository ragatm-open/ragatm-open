# Politique comparative — classification, doctrine et coût

**Statut : implémenté, activé par défaut.** Public : développeurs backend, exploitants,
contributeurs benchmark.

Une question comparative reçoit un gabarit de réponse dédié (politique `compare`). Ce document
définit **quand** cette politique s'applique, **pourquoi elle ne s'applique pas plus largement**,
et **ce qu'elle coûte**. Le mécanisme d'ensemble (sur-abstention, priorité des politiques) vit
dans [answerability.md §1bis](answerability.md).

---

## 1. Doctrine

> Une question mentionnant un **écart**, une **différence observée** ou une **anomalie** n'est pas
> nécessairement comparative.

`compare` exige **deux objets explicitement mis en relation**. Un terme isolé du vocabulaire de
l'écart décrit un **constat** — ce que le corpus a relevé —, pas une demande de comparaison.

Cette règle n'est pas une précaution théorique. Première version de l'heuristique : déclenchement
sur un lexème isolé (`écart`, `différent`, `avantage`, `distinguer`…). Mesuré sur « Quels contrôles
ont montré des écarts ou des limites ? » — une question de findings :

| | Politique standard | Gabarit comparatif (faux positif) |
|---|---|---|
| Structure | liste de 3 constats | « Points communs / Différences » **entre les sources** |
| Rattachement | écarts attribués à Valteria et à Tessaly | attribution perdue |
| Fidélité | aucune affirmation ajoutée | « Aucune action correctrice majeure n'a été nécessaire » — **non étayé** |
| Latence | 5,8 s | 12,9 s |

Un faux positif comparatif coûte donc plus qu'il ne rapporte. **En cas de doute : pas de
`compare`** — la politique standard ou explicative est le repli le moins risqué.

### 1bis. Le VERBE « comparer » n'est pas du vocabulaire de constat

**Statut : implémenté, 30/07/2026.**

La doctrine ci-dessus vise le vocabulaire **nominal** de l'écart (« différence », « écart »,
« avantage ») : ces mots *décrivent* le plus souvent ce qu'un document a relevé. Le verbe
**« comparer » à l'impératif** n'appartient pas à cette famille — c'est une **consigne**, pas un
constat. Exiger de lui deux objets nommés renvoyait le gabarit factuel à des demandes de
comparaison parfaitement explicites : « Compare les risques entre les deux dispositifs. »

Deux niveaux de décision coexistent donc désormais :

| Confiance | Condition | `objects` | Conséquence |
|---|---|---|---|
| `explicit` | deux objets **nommés** mis en relation | les deux objets | politique `compare` **+** expansion déterministe de requêtes (§3) |
| `implicit` | verbe « comparer / confronter » **en tête de demande**, suivi d'un objet | `None` | politique `compare` **seule** — aucune expansion déterministe |

Le garde-fou tient en une ligne : **rien n'est deviné.** `objects` reste `None`, donc la recherche
suit exactement le chemin analytique historique. Seule la consigne de prompt change.

Trois gardes écartent les emplois **non impératifs** du verbe :

- **position** — « Le rapport compare plusieurs valeurs. » : le verbe a un sujet en tête, c'est la
  description de ce que fait un document ;
- **accent final** sur le texte original — « le taux **comparé** à l'an dernier » est un participe
  passé, que le repliage des accents rendrait identique à « compare » ;
- **objet requis** — « compare » seul ne demande rien.

Le nom (« comparaison », « comparatif ») reste soumis à l'exigence de deux objets : « comparaison
des architectures » et « Tableau comparatif des deux approches » ne déclenchent toujours pas.

---

## 2. Motifs reconnus

Détection insensible à la casse et aux accents, avec bornes de mots (`vs` ne peut pas matcher à
l'intérieur d'un mot). Chaque motif capture **exactement deux objets**.

| Motif | Langue | Exemple |
|---|---|---|
| `relation_entre_fr` | FR | *différences / écarts / points communs / similitudes / comparaison / distinctions* **entre A et B** |
| `respectifs_fr` | FR | *avantages (et inconvénients) respectifs de A et de B* |
| `compare_verbe_fr` | FR | *compare(r/z) A **et / avec / à / au** B* |
| `en_quoi_differe_fr` | FR | ***en quoi** A diffère(-t-il) **de** B* |
| `relation_between_en` | EN | *differences / similarities / comparison / gaps* **between A and B** |
| `compare_verb_en` | EN | *compare(d) A **with / to / and / against / versus** B* |
| `versus` | FR + EN | *A **vs** B*, *A **versus** B* |
| `compare_demande_fr` | FR | ***Compare / Comparez / Confronte** …* — objets implicites, cf. §1bis |

`relation_entre_fr` couvre aussi les collocations **« avantages et inconvénients »** et
**« similitudes et différences »**, soumises à la même exigence de deux objets coordonnés.

`en_quoi_differe_fr` tient tout entier dans son amorce : c'est **« en quoi »** qui sépare la demande
de comparaison du constat d'écart. Sans elle, « quels éléments diffèrent de la procédure
attendue ? » reste hors périmètre.

**Retirés comme déclencheurs autonomes** (première version) : `écart`, `écarts`, `différent(e)(s)`,
`distinguer`, `distinction(s)`, `avantage(s)`, `inconvénient(s)`, `similaire(s)`, `similitude(s)`,
`comparatif`. Ces termes ne déclenchent plus que **dans** un motif structuré ci-dessus.

### Validation des objets

Un objet capturé n'est retenu que s'il est : non vide ; porteur d'au moins un mot plein de ≥ 2
caractères (pas uniquement des mots vides) ; **distinct** de l'autre après normalisation.

Exception, pour les motifs **ancrés** uniquement (tous sauf `versus`) : une étiquette d'un seul
caractère est admise. Dans « différences entre **A** et **B** », la charpente du motif a déjà
établi que « A » est un objet ; l'opérateur nu « A vs B » n'a, lui, aucune amorce pour le
confirmer et garde son plancher à deux caractères.

Les deux
objets alimentent ensuite l'expansion de requêtes (§3) — c'est ce qui rend le routage *et* la
recherche solidaires : sans objets identifiables, il n'y a rien à comparer **ni** rien à chercher.

---

## 3. Expansion de requêtes — déterministe, sans LLM

Auparavant, une question comparative activait le mode analytique, donc une **reformulation LLM**.
Mesuré : **+1,8 s médian** sur le retrieval seul (jusqu'à ×25 sur un cas), pour une requête non
déterministe qui rebattait le top-k sans gain de couverture mesurable.

Une comparaison explicite porte déjà ses deux objets : il n'y a rien à deviner. L'expansion est
donc **déterministe** — `build_comparative_queries` :

```
requête 1 = la question originale
requête 2 = « objet A objet B »   (les deux objets ENSEMBLE)
```

**Aucun mot n'est ajouté** : ni acronyme développé, ni traduction, ni terme métier — seulement les
deux segments déjà présents dans la question. Plafonnée par `RAG_MAX_SEARCH_QUERIES`, dédupliquée,
sans requête vide. **Zéro appel LLM sur le chemin comparatif** — ce qui rend inutile tout réglage
de timeout dédié.

### Pourquoi une requête combinée, et non une par objet

Choix **mesuré** sur les 10 questions comparatives du jeu de benchmark (même corpus, même index,
mêmes seuils) — critère : les deux objets sont-ils présents dans les chunks transmis ?

| Stratégie | Deux objets couverts | Chunks objet 1 | Chunks objet 2 |
|---|---|---|---|
| Question seule (aucune expansion) | 9/10 | 63 | 65 |
| Question + objet A + objet B | 9/10 | 53 | 63 |
| **Question + « A B »** | **10/10** | **67** | **70** |

Deux requêtes séparées **dégradent** la couverture : à la fusion, chaque jeu de résultats évince
l'autre, et le premier objet perd des chunks (53 contre 67). La requête combinée remonte au
contraire les passages qui traitent des deux côtés à la fois — ceux qui permettent réellement de
comparer.

Les autres chemins sont inchangés : le mode explicatif conserve sa reformulation LLM (avec son
repli silencieux sur la question d'origine en cas d'échec ou de timeout), et une question standard
ne déclenche toujours aucune expansion.

---

## 4. Limites assumées

Toutes se replient du côté sûr — **jamais de faux positif**, au prix d'un `compare` manqué :

| Formulation | Décision | Raison |
|---|---|---|
| « comparaison des résultats » | pas `compare` | un seul objet — comparé à quoi ? |
| « Tableau comparatif des deux approches » | pas `compare` | forme **nominale** : aucun objet nommé |
| « Quels points communs entre les deux dispositifs ? » | pas `compare` | forme nominale sans coordination « entre X **et** Y » |
| « différences entre A, B et C » | pas `compare` | énumération à trois termes, hors périmètre |
| « écarts constatés entre deux périodes » | pas `compare` | « deux périodes » n'est pas deux objets nommés |
| « Quels éléments diffèrent de la procédure attendue ? » | pas `compare` | sans l'amorce « en quoi », indistinguable d'un écart à un attendu |
| « A vs B » (lettres seules) | pas `compare` | opérateur nu, signal trop faible ; « Tessaly vs Valteria » passe |
| « Compare la procédure a la réalité » | `compare` **implicite** | « à » sans accent : aucun objet extrait, mais le verbe de demande tranche (§1bis) |

Deux limites ont été **levées** le 30/07/2026, et pour la même raison — le verbe de demande est
une consigne, pas un constat (§1bis) : « Compare les deux dispositifs » (désormais `compare`
implicite, sans objets) et « En quoi X diffère-t-il de Y ? » (désormais `compare` explicite, avec
objets, grâce au motif `en_quoi_differe_fr`).

---

## 5. Budgets analytiques

Les modes explicatif **et** comparatif partagent les budgets `RAG_EXPLAIN_*`. Ces réglages étaient
lus par l'orchestrateur sans être déclarés : les poser dans `.env` n'avait **aucun effet**, alors
que la documentation annonçait des budgets distincts.

Ils sont désormais **déclarés**, avec des **défauts égaux aux budgets standards** — la déclaration
ne change donc rien au comportement, elle le rend lisible et calibrable :

| Variable | Défaut | Application |
|---|---|---|
| `RAG_EXPLAIN_TOP_K` | `RAG_TOP_K` | `max()` — ne peut qu'élargir |
| `RAG_EXPLAIN_VECTOR_TOP_K` | `RAG_VECTOR_TOP_K` | `max()` |
| `RAG_EXPLAIN_MIN_TEXT_RANK` | `RAG_MIN_TEXT_RANK` | `min()` — ne peut qu'abaisser |
| `RAG_EXPLAIN_RERANK_TRIGGER_MIN_CANDIDATES` | `RAG_RERANK_TRIGGER_MIN_CANDIDATES` | `min()` |
| `RAG_EXPLAIN_MIN_SCORE` | **non défini** (optionnel) | `min()` — le figer plafonnerait aussi les espaces portant un seuil plus élevé |

Un réglage **inerte** (plus petit que le budget standard, ou seuil plus haut) est signalé au
démarrage par le system check `rag_core.W001` ; une taille non positive est bloquante
(`rag_core.E005`). Valeurs de référence : [variables d'environnement](../10-reference/variables-environnement.md).

---

## 6. Ce que la politique comparative NE fait pas

- Elle **ne contourne aucune garde** : garde analytique Excel, éligibilité documentaire,
  permissions et politique d'egress s'appliquent avant toute génération.
- Elle **n'améliore pas le retrieval des constats négatifs** : ce défaut est mesuré, documenté, et
  reste à traiter au niveau du retrieval (cf. [relance-anti-sur-abstention.md §4](relance-anti-sur-abstention.md)).
- Elle **n'élargit pas le contexte** : `top_k` inchangé par défaut.

---

## Références d'implémentation

- `backend/apps/rag_core/services/query/heuristics.py` (`detect_comparative_intent`)
- `backend/apps/rag_core/services/query/rewrite.py` (`build_comparative_queries`)
- `backend/apps/rag_core/services/query/prompt_builder.py` (préambule `compare`)
- `backend/apps/rag_core/checks_analytical_budgets.py`
- `backend/config/settings/base.py` (budgets analytiques déclarés)
- `tests/tests_root/unit/test_comparative_intent.py` (doctrine)
- `tests/tests_root/unit/query/test_comparative_query_expansion.py` (zéro appel LLM)
- `tests/tests_root/integration/test_comparative_policy_precedence.py` (priorité et gardes)
