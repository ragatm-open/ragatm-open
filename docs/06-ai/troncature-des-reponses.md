# Troncature des réponses

*Statut : **corrigé le 23/08/2026**.* Public : développeurs backend, exploitants.

---

## 1. Le défaut

Le chat génère sous un plafond de tokens (`RAG_FACTUAL_NUM_PREDICT`). À **768**, ce
plafond tombait exactement sur le **p90** des réponses réelles : les plus longues
étaient coupées **au milieu d'une phrase**.

Mesuré sur 200 réponses de chaque environnement :

| | réponses coupées | longueur médiane | p90 |
|---|---:|---:|---:|
| production | **12 / 200 — 6 %** | 606 c | 2 724 c |
| staging | **40 / 200 — 20 %** | 2 585 c | 3 168 c |

## 2. Pourquoi personne ne le voyait

La ligne `Sources: [1], [2]` **n'est pas générée par le modèle** : elle est ajoutée
après coup par `finalize_answer`. Une réponse coupée en plein mot reçoit donc un pied
de page bien formé **et** son badge vert de citations.

```
… conduit à une **exp

Sources: [3], [4]
```

Le modèle s'est arrêté sur `**exp` ; le système a proprement conclu à sa place. Rien,
dans l'interface ni dans l'audit, ne distinguait cette réponse d'une réponse finie.

## 3. Le signal, déterministe

La Gateway **déclare** la raison de son arrêt, dans le champ `done_reason` de
`GenerateResponse` — et dans l'événement terminal de `/v1/generate/stream`. Vérifié
contre la Gateway de production le 23/08/2026, sur les deux régimes :

```
num_predict=16    ->  done_reason='length'   completion_tokens = 16    texte coupé
num_predict=4096  ->  done_reason='stop'     completion_tokens = 832   texte complet
```

> `done_reason == 'length'` **est** la troncature, dite par celui qui l'a provoquée.

Le champ `finish_reason` porte le même vocabulaire ; les deux sont lus.

### Correction du 23/08/2026

Cette page a d'abord affirmé que *« la Gateway ne renvoie aucune raison d'arrêt —
`done: true` quel que soit le cas »*. C'était faux, et pour une raison instructive :
`done` avait été lu, `done_reason` — juste à côté, dans la même réponse — ne l'avait pas
été. La détection reposait donc sur une inférence là où une déclaration existait.
Signalé par l'audit `AIGW-WORKLOAD-ISOLATION` conduit côté Gateway.

L'inférence reste, en **repli** : quand aucune raison n'est déclarée — un fournisseur
muet, un libellé inconnu — atteindre exactement le plafond ne peut pas être une
coïncidence de fin de phrase.

```
completion_tokens >= num_predict      (repli, si done_reason absent ou non reconnu)
```

Une raison déclarée n'est jamais recalculée par-dessus : un compteur qui la contredirait
serait un défaut à corriger chez le fournisseur, pas une raison de le désavouer ici. Un
libellé inconnu (`load`, autre) n'est **pas** traité comme « complet » — il retombe sur
le repli.

Aucune heuristique de ponctuation n'intervient, et aucune ne serait fiable : une réponse
peut légitimement finir sans point.

`answer_was_truncated()` rend **`False` si l'information manque** : on ne signale pas
une troncature qu'on n'a pas constatée. Un faux positif apprendrait à l'utilisateur à
ignorer l'avertissement.

Le contrat de réponse porte donc `answer_truncated: true | false | null`, où **`null`
signifie « inconnu »** — à ne pas confondre avec `false`.

## 4. Le plafond, redimensionné sur mesure

| | p90 | p99 | plafond actuel |
|---|---:|---:|---:|
| production | 649 t | 1 594 t | 768 |
| staging | 750 t | 1 026 t | 768 |

**2 048** couvre le p99 des deux environnements et vaut déjà pour les synthèses
(`RAG_REPORT_NUM_PREDICT`).

La fenêtre du modèle n'a jamais été la contrainte : `qwen3:30b-instruct` accepte
**262 144 tokens** et `RAG_FACTUAL_NUM_CTX` en alloue 8 192. Comme pour le budget de
prompt la veille, c'était un défaut de prudence jamais rejugé quand le modèle a changé.

### Ce que relever le plafond coûte

Une génération plus longue occupe le GPU plus longtemps, et la Gateway **sérialise**
la documentation d'exploitation (hors périmètre de cette édition)).
À trafic actuel c'est sans conséquence ; ce ne le resterait pas.

### Un effet de bord à connaître

Relever le budget de génération **réduit** le budget de prompt admissible :
`(num_ctx − num_predict) × 3,5`, soit **21 504** caractères contre 26 040 auparavant.
Les 8 300 de production et de staging restent très en deçà. Le check `rag_core.W003`
surveille cette borne.

## 5. Ce que le correctif ne fait pas

Relever le plafond **réduit** la fréquence, il ne supprime pas le phénomène. C'est
pourquoi les deux moitiés du lot vont ensemble : un plafond mieux réglé qui coupe encore
1 % des réponses **en silence** resterait le même défaut, en plus rare.

L'avertissement affiché n'est pas décoratif : il est la partie qui rend le défaut
résiduel supportable.

---

## Références d'implémentation

- `backend/apps/rag_core/services/answer_contract.py` (`answer_was_truncated`, `declared_stop_reason`)
- `backend/apps/chat/services/chat_service.py` (chemins synchrone et flux)
- `backend/config/registry/domains/generation.py` (`RAG_FACTUAL_NUM_PREDICT`)
- `frontend/src/app/chat/page.tsx` (avertissement), `_lib/citations.ts` (contrat)
- `tests/tests_root/unit/test_answer_truncation.py`
