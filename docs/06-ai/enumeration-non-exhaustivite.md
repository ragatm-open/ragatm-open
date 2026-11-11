# Énumérations documentaires — évaluation de la non-exhaustivité

**Statut : évalué, NON implémenté.** Aucun code de ce lot ne modifie le contrat de
réponse. Ce document instruit un besoin ; toute implémentation doit faire l'objet d'un
commit et d'une PR séparés. Public : développeurs backend, produit.

---

## 1. Le problème

Une question de forme énumérative —

```
Quelles anomalies ont été constatées ?
Liste toutes les insuffisances.
Quels sont les écarts documentés ?
Présente l'ensemble des limites.
```

— demande implicitement **l'intégralité** d'une population. Le moteur, lui, transmet au
modèle **8 passages**. La réponse produite se lit comme une liste complète (« Les
anomalies effectivement observées sont : 1… 2… ») alors qu'elle porte sur une fraction
du fonds.

Ce n'est pas un défaut de retrieval : c'est un **désaccord de forme** entre ce que la
question demande et ce que le mécanisme peut fournir.

---

## 2. Mesure

Population éligible (documents `READY`, espace non supprimé), mesurée en dev :

| Corpus | Chunks porteurs de vocabulaire de constat | Documents concernés | Total chunks | Total documents |
|---|---:|---:|---:|---:|
| CM-2026 | **994** | **180** | 27 086 | 904 |
| Mission | **561** | **109** | 8 100 | 497 |

| Grandeur | CM-2026 | Mission |
|---|---:|---:|
| Créneaux de contexte (`top_k`) | 8 | 8 |
| **Couverture maximale possible, en passages** | **0,8 %** | **1,4 %** |
| **Couverture maximale possible, en documents** | **≤ 4,4 %** | **≤ 7,3 %** |

**Variabilité** : sur une question d'anomalies rejouée 3 fois (température 0,1),
**2 réponses distinctes sur 3**. Deux utilisateurs posant la même question obtiennent
des listes différentes — sans qu'aucune ne soit fausse.

**Risque d'interprétation** : élevé. Rien dans la réponse actuelle ne signale que
l'énumération est partielle. Le contrat de sources indique bien « N sources citées »,
mais rien ne dit combien de documents du fonds portaient une réponse possible.

---

## 3. Précédent interne

Le produit **refuse déjà** cette forme de question sur les classeurs : la garde
analytique Excel bloque l'intention `exhaustive_enumeration` avant tout appel au modèle,
au motif que « le LLM ne verrait que le top-k, pas le classeur entier »
(cf. [answerability §3](answerability.md)).

Le raisonnement vaut à l'identique pour les documents : ce qui change, c'est seulement
que la population n'est pas bornée par un fichier. La cohérence plaide pour un
traitement explicite — pas nécessairement un refus.

---

## 4. Contrat proposé (à implémenter séparément)

Trois options, par ordre de coût croissant :

| Option | Comportement | Coût | Risque |
|---|---|---|---|
| **A. Mention de non-exhaustivité** | ajouter au contrat de réponse un signal `answer_is_exhaustive=false`, rendu par l'UI : « Voici les principaux éléments retrouvés dans les sources sélectionnées. Cette liste peut ne pas être exhaustive. » | faible | aucun sur le retrieval |
| **B. Mention chiffrée** | « Réponse partielle — 8 passages parmi N documents consultés. » | faible + compteur | annoncer un dénominateur qu'on ne sait pas calculer sans coût |
| **C. Refus cadré** | comme la garde Excel : proposer de restreindre le périmètre | moyen | perte d'usage sur des questions légitimes |

**Recommandation : option A.** Elle corrige l'interprétation sans rien retirer, ne touche
ni au retrieval ni au prompt, et se règle entièrement dans le contrat de réponse — la
couche qui porte déjà la distinction « sources retrouvées / transmises / citées »
(cf. [politiques-llm.md §6](politiques-llm.md)).

L'option B suppose de compter les documents « porteurs d'une réponse possible », ce que
le moteur ne sait pas faire sans une classification fiable des passages — **prérequis
non disponible** (le classifieur lexical a été mesuré non fiable).

---

## 5. Périmètre d'une future implémentation

- détection d'**intention d'énumération** (générique : *liste*, *tous / toutes*,
  *l'ensemble des*, *quels sont les* + pluriel) — aucune logique métier, aucun
  vocabulaire d'anomalie ;
- signal porté par le **contrat de réponse**, jamais par le prompt ;
- rendu UI discret, non bloquant ;
- tests : détection, non-déclenchement sur question factuelle, rendu, non-régression du
  contrat de sources ;
- documentation.

**Ce chantier ne doit pas être mêlé à un correctif de classement.**

---

## Références d'implémentation

- `backend/apps/rag_core/services/answer_contract.py` (contrat de réponse existant)
- `backend/apps/chat/services/excel_guard.py` (précédent : `exhaustive_enumeration`)
- `docs/06-ai/answerability.md`
