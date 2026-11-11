# Dérive de famille de pipeline

*Statut : **implémenté le 23/08/2026**.* Public : développeurs backend, exploitants.

Complète la documentation d'exploitation (hors périmètre de cette édition).

---

## 1. Le mode de défaillance

Dans un espace **gouverné**, le leg vectoriel ne compare la question qu'aux fragments
portant **exactement** la même famille de pipeline :

```sql
AND c.pipeline_family = %(pf)s
```

La famille de la question est dérivée, à chaque requête, des champs attestés par la Gateway
(`model_logical` + `model_digest` vérifié — jamais découpée du `pipeline_id`, qui est opaque).
Celle des fragments a été écrite à l'indexation. Rien ne garantit qu'elles restent égales : la
Gateway peut changer les **poids** d'un modèle sans changer son **nom**, et le digest attesté
change alors.

Le filtre fait alors exactement ce qu'on lui demande — il ne trouve rien.

> Et **zéro fragment vectoriel est indistinguable d'un espace qui n'a pas la réponse.**

Le système répond « je ne sais pas », poliment, sur un corpus qui contient la réponse.
Aucune erreur, aucune trace, aucune alerte. C'est le pire des trois résultats possibles :
une panne franche se voit, une mauvaise réponse se conteste, un **mensonge plausible**
s'accepte.

## 2. Ce que la garde précédente couvrait — et pas

Une garde existait déjà, et elle est bonne : une famille **absente** de la réponse
d'embeddings fait échouer la recherche (`RetrievalInvariantError`, fail closed), plutôt
que de la laisser dégrader en repli lexical silencieux.

Elle ne dit rien du cas où la famille est **présente mais différente**. C'est pourtant
le cas le plus probable : il ne demande aucune panne, seulement une mise à jour de modèle
côté Gateway.

## 3. La garde

Déclenchée **uniquement** sur la combinaison suspecte — espace gouverné, aucune erreur,
zéro candidat vectoriel, famille de question connue :

```
inventaire = familles réellement présentes parmi les fragments COMPARABLES
             (mêmes conditions d'éligibilité et de cohérence que la recherche,
              moins le filtre de famille — c'est lui qu'on explique)

si l'inventaire n'est pas vide
   et que la famille de la question n'y figure pas
       -> journal `retrieval_pipeline_family_drift`
       -> RetrievalInvariantError (FAIL CLOSED)
```

Trois propriétés à retenir :

| | |
|---|---|
| **coût nul en régime normal** | la requête d'inventaire n'est émise que si la recherche n'a rien trouvé |
| **un espace vide reste vide** | sans fragment comparable, pas de conclusion — zéro source n'est pas zéro source *pour cette raison* |
| **un document à réindexer ne compte pas** | il n'est pas comparable ; l'inclure annoncerait une dérive là où il n'y a qu'une attente |

Le message nomme la famille attendue **et** celles trouvées : un diagnostic qui dit
« quelque chose ne va pas » sans dire quoi oblige à refaire l'enquête.

Une défaillance de la sonde elle-même rend un inventaire vide : le diagnostic n'aboutit
pas, la recherche reste vide, mais on n'invente pas une dérive — et on ne transforme pas
une panne de diagnostic en panne de recherche.

## 4. Périmètre

Un espace **non gouverné** est strictement inchangé : la garde ne s'arme que si
`space_is_governed(space)`. Un test le vérifie sur la combinaison exacte qui déclenche
la garde en mode gouverné.

## 5. Que faire quand elle se déclenche

C'est un **constat d'identité**, pas un incident réseau. Deux causes possibles :

1. la Gateway a changé les poids du modèle → le corpus doit être **ré-embeddé** (à corpus
   constant, la documentation d'exploitation (hors périmètre de cette édition)) ;
2. l'espace a été migré à moitié → vérifier avec `manage.py reembed_preflight`.

Dans les deux cas, `manage.py check_embedding_capabilities` et le rapport de préflight
disent quelle identité la Gateway sert aujourd'hui.

---

## Références d'implémentation

- `backend/apps/rag_core/services/query/candidate_retriever.py` (garde, journal `retrieval_pipeline_family_drift`)
- `backend/apps/rag_core/repositories/chunk_repository.py` (`pipeline_families_present`)
- `tests/tests_root/unit/query/test_pipeline_family_drift.py`
