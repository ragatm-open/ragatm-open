# Relance après abstention — rapport d'évaluation (avant implémentation)

**Statut : évalué, NON implémenté.** Aucun code de relance n'existe dans le chemin live ; aucun
flag `RAG_ANTI_OVERREFUSAL_ENABLED` n'est déclaré à ce jour. Ce document est le rapport
d'instruction demandé avant toute décision. Public : développeurs backend, exploitants.

Contexte et correctifs déjà livrés (politique `compare`, contrat de réponse) :
[answerability.md §1bis](answerability.md).

---

## 1. Question posée

Quand le modèle s'abstient (« Je ne sais pas. ») alors que le retrieval a fourni des sources,
faut-il **relancer une génération** avec une politique de prompt plus permissive
(`partial` / `explain` / `compare`) ?

La règle naïve — *abstention **ET** `nb_sources >= N`* — est **rejetée d'emblée** : le nombre de
sources ne dit rien de leur pertinence. Le pipeline retient toujours au moins `RAG_MIN_SOURCES`
extraits dès qu'un chunk franchit le seuil ; un pool de 8 extraits hors sujet satisfait la
condition aussi bien qu'un pool de 8 extraits probants. Relancer sur ce seul critère
transformerait une abstention **correcte** en réponse bavarde — exactement le risque que la
politique d'ancrage cherche à écarter.

---

## 2. Mesure de référence (poste de développement)

Source : `rag_query_log` de l'environnement de développement, **281 requêtes** cumulées.

| Policy | Requêtes | Abstentions | Taux | Sources moy. |
|---|---:|---:|---:|---:|
| `ok` | 193 | 91 | **47 %** | 7,7 |
| `low_confidence` | 31 | 9 | 29 % | 7,2 |
| `explain` | 16 | 2 | **12,5 %** | 7,9 |
| `summary` | 30 | 0 | 0 % | 1,0 |
| `news` | 5 | 0 | 0 % | 5,0 |
| `no_answer` | 4 | 0 | — | 0 |
| `excel_analytics_post_retrieval` | 2 | 0 | — | 8,0 |

Lectures :

- **Le gabarit de prompt pèse plus que le fonds** : 47 % d'abstention sous `ok` contre 12,5 %
  sous `explain`, à corpus et modèle identiques (`llama3.1:8b` via AI Gateway).
- **Les abstentions ne manquent pas de sources** : sur les 91 abstentions `ok`, **89 disposaient
  d'au moins 4 sources** et **aucune n'avait zéro source**. Le critère « nb_sources » ne
  discrimine donc rien du tout sur cette population — il est vrai partout.
- **La politique `compare` ne couvre qu'une petite part** : seules **4 des 91** abstentions `ok`
  (4,4 %) portent une formulation comparative. Le correctif livré traite un cas réel et
  reproductible, **pas** le gros du phénomène.
- **Détection d'abstention** : le préfixe strict historique voyait 102 abstentions ; le détecteur
  centralisé en voit ~106 sur le même échantillon (+4 %). L'écart est réel mais marginal — la
  formule « je ne sais pas » domine largement, le modèle suivant la consigne du gabarit à la
  lettre.

### Limites de cette mesure

- Échantillon **mono-environnement, mono-utilisateur**, corpus métier unique (CM-2026) : c'est un
  ordre de grandeur, pas une base de décision statistique.
- `RagQueryLog.latency_ms` est renseigné **à la création du log**, dans `query_rag` : il mesure le
  **retrieval**, pas la génération (`maybe_update_log` ne le met jamais à jour). **Aucune mesure
  de latence de génération n'existe aujourd'hui en audit** — c'est un prérequis d'instrumentation
  avant de chiffrer le coût d'une relance (cf. §6).
- Aucune vérité terrain : « le modèle aurait-il pu répondre ? » n'est pas mesuré ici. Le taux de
  **sur**-abstention (abstention injustifiée) reste **inconnu** ; seules 47 % d'abstentions brutes
  sont établies.

---

## 3. Critères d'éligibilité proposés

Une relance ne doit être envisagée que si **tous** les critères sont satisfaits. Colonne
« signal » = existe-t-il aujourd'hui, ou faut-il le construire ?

| # | Critère | Pourquoi | Signal disponible ? |
|---|---|---|---|
| 1 | Réponse initiale reconnue comme **abstention** | condition d'entrée | ✅ `answer_contract.is_abstention` |
| 2 | Policy initiale **autorisant** la relance (`ok` uniquement) | `partial` / `low_confidence` / `explain` / `compare` ont **déjà** un préambule permissif : relancer serait répéter | ✅ `answer_kind` |
| 3 | Retrieval **non vide** | sans extrait, la relance ne peut qu'halluciner | ✅ `sources` |
| 4 | **Scores suffisants** — `best_score ≥ min_score` du RagSpace, et non pas seulement `≥ 0` | un pool franchi de justesse est un pool douteux | ✅ `sources[].score`, `min_score` résolu |
| 5 | Answerability **non négative** | le module déterministe P1.8 sait dire « les preuves ne suffisent pas » | ⚠️ existe mais **offline** — brancher en lecture seule d'abord ([answerability §2](answerability.md)) |
| 6 | Aucune **garde de sécurité / permission** armée | owner-scope, egress, quotas : une relance ne doit jamais rouvrir une porte fermée | ✅ décisions en amont du prompt |
| 7 | Aucun **refus tabulaire ou déterministe** (garde Excel, moteur tabulaire, `no_answer`) | ces refus sont des décisions de **correction**, pas des hésitations de modèle | ✅ `excel_guard`, `answer_kind` |
| 8 | Les sources **couvrent réellement** les concepts principaux de la question | seul critère qui distingue « le modèle a renoncé » de « le fonds ne contient pas la réponse » | ❌ **à construire** (cf. §4) |
| 9 | **Aucune relance déjà effectuée** pour ce tour | borne dure : une relance, jamais deux | ❌ à porter dans le contexte de requête |

Le critère **8** est le cœur du dispositif. Sans lui, il ne reste que des conditions
d'exclusion : elles évitent les relances absurdes mais ne prouvent jamais qu'une relance est
**justifiée**.

---

## 4. Mesure de couverture des concepts comparés

Objectif : distinguer deux situations que le log confond aujourd'hui.

| Situation | Signature attendue | Comportement souhaitable |
|---|---|---|
| **Sur-abstention du modèle** | les concepts clés de la question sont couverts par les sources, avec des passages **affirmatifs** | relance légitime |
| **Retrieval incomplet** | une seule entité couverte, ou couverture uniquement par des titres / questions / mentions incidentes | **pas** de relance : réponse partielle explicite |

Briques nécessaires :

1. **Extraction des entités/options comparées** — pour une question comparative, isoler les termes
   coordonnés (« entre X et Y », « X vs Y »). Réutilisable : `strong_signals.extract_identifiers`
   sait déjà repérer les identifiants forts.
2. **Couverture par entité** — chaque entité doit apparaître dans au moins un chunk, et le
   comptage doit être **pondéré IDF** plutôt que binaire : `benchmark/answerability.py` fournit
   déjà `weighted_coverage` et `build_corpus_df`.
3. **Couverture par critère** — au moins *k* dimensions communes documentées pour les deux
   entités, sinon la comparaison serait construite sur un seul côté.
4. **Qualité du passage** — écarter les extraits qui ne sont qu'un **titre**, une **question** ou
   une **mention incidente**. Signal observé dans le cas fondateur : la source n°1 retenue était
   `Questions-a-poser-Valteria.txt`, c'est-à-dire une liste de **questions**, pas une affirmation.
   Un extrait interrogatif ne peut étayer aucune réponse.

Sortie attendue, sérialisable et auditable : `{entities: [...], covered: [...], missing: [...],
affirmative_ratio: 0.xx}`. Elle sert d'abord à **mesurer**, avant même de servir à décider.

---

## 5. Risques de faux positifs

| Risque | Mécanisme | Atténuation proposée |
|---|---|---|
| **Réponse inventée** | la relance permissive pousse à répondre ; le modèle comble avec ses connaissances générales | préambule `partial`/`compare` (déjà écrit) + garde-fous de citation ; **exiger** au moins une citation valide dans la réponse relancée, sinon conserver l'abstention initiale |
| **Abstention correcte écrasée** | le fonds ne contient réellement pas la réponse | critère 8 (couverture) obligatoire ; conserver la réponse relancée seulement si elle cite |
| **Détection d'abstention trop large** | une réponse partielle légitime prise pour une abstention → relance inutile | détecteur centralisé mesuré sur le texte **restant** ; couvert par `tests/tests_root/unit/test_answer_contract.py` |
| **Contournement d'une garde** | relancer après un refus tabulaire ou de permission | critères 6 et 7, et **jamais** de relance sur `status=no_answer` |
| **Boucle / amplification** | relance en cascade sur un modèle instable | critère 9 (une seule relance), borne dure côté code, pas côté prompt |
| **Incohérence de fil** | le tour affiche une abstention puis une réponse | la relance doit se produire **avant** la persistance et l'émission `done` ; en SSE, ne rien émettre tant que la décision n'est pas prise |

---

## 6. Impact attendu sur les benchmarks et la latence

- **Latence** : une relance = **un appel LLM complet supplémentaire**. Sur la population concernée
  (abstentions `ok`, soit 47 % des requêtes `ok` en dev), le temps de réponse serait
  approximativement **doublé** pour ces tours. Aucun chiffre fiable n'est disponible aujourd'hui :
  la latence de génération n'est pas journalisée (§2). **Prérequis : instrumenter la latence de
  génération** dans `RagQueryLog` avant d'ouvrir le flag.
- **Coût** : doublement des tokens de prompt sur les tours relancés (le contexte est réémis en
  entier). Le prompt vaut jusqu'à `RAG_MAX_PROMPT_CHARS=18000` caractères.
- **Benchmarks** : la relance déplace le curseur précision/rappel dans le sens du **rappel**. Le
  benchmark answerability mesure aujourd'hui une précision no-answer de **0,63** (cf.
  ../08-testing/benchmark-rag.md) ; une relance non filtrée la
  dégraderait mécaniquement. Toute campagne devra publier les deux versants — pas seulement le
  gain de réponses.
- **Sans relance**, une part du problème est déjà traitée à coût nul : choisir la bonne politique
  **du premier coup** (politique `compare`) ne coûte aucun appel supplémentaire. C'est la voie à
  épuiser en premier.

---

## 7. Contrat prévu si la relance est un jour implémentée

Flag (défaut **off**) :

```env
RAG_ANTI_OVERREFUSAL_ENABLED=0
```

Événement d'audit attendu :

```json
{
  "initial_policy": "ok",
  "initial_answer_kind": "abstention",
  "retry_triggered": true,
  "retry_policy": "partial",
  "retry_reason": "abstention_with_eligible_sources"
}
```

Invariants non négociables : une seule relance par tour ; jamais de contournement d'un
`no_answer` explicite, d'une garde de permission, de sécurité ou de calcul ; politique de relance
choisie selon l'intention (`partial` / `explain` / `compare`) ; **réutilisation des sources et
scores du premier retrieval** — aucune seconde recherche sans justification mesurée.

---

## 8. Recommandation

1. **Mesurer d'abord, décider ensuite.** Trois instrumentations manquent : latence de génération
   en audit, couverture par entité (§4), et lecture *shadow* de l'answerability déterministe. Elles
   sont toutes non intrusives et n'exigent aucun changement de comportement.
2. **Épuiser le levier gratuit** : mesurer l'effet réel de la politique `compare` et du contrat de
   réponse sur le taux d'abstention avant d'ajouter un appel LLM. Les 4,4 % de questions
   comparatives ne sont qu'un début : la même instruction (« ne réponds pas *Je ne sais pas* si des
   sources sont pertinentes ») peut être étendue au gabarit `ok` **sans relance**, ce qui coûte
   zéro token supplémentaire. Cette piste devrait être évaluée **avant** la relance.
3. **N'ouvrir le flag qu'avec le critère 8 opérationnel.** Sans mesure de couverture, la relance ne
   serait qu'un « réponds quand même », c'est-à-dire l'inverse de la politique d'ancrage du
   produit.

---

## Références d'implémentation

- `backend/apps/rag_core/services/answer_contract.py` (détection d'abstention, contrat)
- `backend/apps/rag_core/services/query/{heuristics,prompt_builder,orchestrator}.py`
- `backend/apps/rag_core/benchmark/answerability.py` (couverture pondérée IDF, offline)
- `backend/apps/audit/models.py` (`RagQueryLog`)
