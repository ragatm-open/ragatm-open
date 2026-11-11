# Politiques LLM — prompts, presets, anti-hallucination, audit

Objectif : décrire la gouvernance des prompts et de la génération : presets, intents, templates
par workspace, garde-fous anti-hallucination, préambules conditionnels, citations déterministes
et audit. Public : développeurs backend, exploitants.

Ce document corrige et remplace l'ancien `llm_policies.md`. Le routage bas-niveau (Gateway, repli,
providers) est décrit dans [providers-modeles.md](providers-modeles.md) ; le chemin chat complet
dans [../03-backend/chat-generation.md](../03-backend/chat-generation.md).

---

## 1. Presets de génération

Le preset **factuel** est le mode nominal du chat (`resolve_model_for_task(TASK_FACTUAL, …)`) :

| Preset | Réglages | Usage |
|---|---|---|
| `factual` | `RAG_FACTUAL_TEMPERATURE=0.1`, `RAG_FACTUAL_NUM_CTX=8192`, `RAG_FACTUAL_NUM_PREDICT=768` | réponse RAG standard |
| `report` | `num_predict=2048` | réponses longues / analyses |
| `rewrite` | prompt court contraint | réécriture de requête |

`think=False` est forcé pour `qwen3:14b` (évite les réponses vides). Le routage par tâche
(modèle par preset) est **activable par flag** `RAG_TASK_ROUTING_ENABLED=0`, cf.
[providers-modeles.md](providers-modeles.md) §6.

---

## 2. Intents (heuristiques de mode)

- **news** (`apps/chat/services/intents/news.py`, `llm_route_news_or_none`) : un petit prompt LLM
  décide si la question porte sur l'actualité. Si oui, la réponse est construite à partir des top N
  documents les plus récents mentionnant le terme (contourne le RAG classique). `answer_kind="news"`.
  L'heuristique regex `news_mode` est **activable par flag** `RAG_ENABLE_NEWS_HEURISTIC` (défaut
  False).
- **listing** (regex dans `api/v1/chat/views.py`) : « liste des documents sur X » court-circuite le
  RAG top-k et interroge le dépôt documentaire.
- **inventory** / **explanatory** (`query/heuristics.py`) : inventory élargit `top_k` + diversité ;
  explanatory relâche les seuils, active la réécriture de requête et un prompt structuré.
  `is_explanatory_question` ne cherche **pas** des mots-clés bruts (*implémenté, 30/07/2026*) :
  elle exige une **forme de demande**. Sept familles de règles, **bilingues FR/EN**, testées
  dans cet ordre — `analytic_intent_family()` rend celle qui a tranché :

  | Famille | Déclencheur | Exemple |
  |---|---|---|
  | `interrogative` | interrogatif explicatif | « pourquoi », « dans quelle mesure », *why*, *to what extent*, *how does* |
  | `idiom` | locution figée | « mets en perspective », « fais ressortir », *draw out*, *put into perspective* |
  | `marker` | marqueur non ambigu | « en détail », « approfondie », *in depth*, *thoroughly* |
  | `analytic_verb` | verbe analytique en tête | « Analyse… », « Évalue… », *Assess…*, *Summarise…* |
  | `production_deliverable` | verbe de production + livrable | « Rédige un rapport… », *Draft a briefing note…* |
  | `nominal_form` | l'énoncé ENTIER est un groupe nominal à tête de livrable | « Synthèse des écarts. », *Executive summary of the key risks.* |
  | `ellipsis` | ellipse autonome | « Plus de détail ? », *A deeper analysis?* |

  C'est ce qui sépare *« Rédige un rapport détaillé »* de *« Quel est le nom du rapport ? »* :
  le mot est le même, la demande ne l'est pas. Cinq gardes portent la distinction — la
  **position** du verbe (« le rapport compare plusieurs valeurs » décrit un tiers), l'**accent
  final** FR et la **terminaison `-ed`** EN (participes passés), la **négation évaluée avant le
  verbe** (« Compare les dates sans analyser les causes » reste comparatif), les **consignes
  citées** (« le document indique : “analyse les risques” »), et l'exclusion des **interrogatifs
  quantitatifs** (*how many*, « combien »).

  Le mot « contexte » seul ne déclenche plus rien : il n'est atteignable que comme LIVRABLE
  demandé (« Présente le contexte réglementaire »).

  **Parité FR/EN** (*30/07/2026*) : avant ce lot, les vocabulaires étaient exclusivement
  français et le rappel `explain` anglais valait **0,018** — la quasi-totalité des demandes
  anglaises d'analyse, de synthèse et de rapport recevait une réponse factuelle. Mesures
  avant/après et validation indépendante :
  benchmark des heuristiques.

---

## 3. Templates de prompt

### Template par défaut (`apps/rag_core/domain/retrieval.py`)

```
Tu es un assistant RAG.
Réponds uniquement à partir des SOURCES.
Si les sources ne permettent pas de répondre, dis exactement : "Je ne sais pas.".

RÈGLES DE CITATION (IMPORTANT):
- Tu ne dois JAMAIS inventer des numéros de sources.
- Les seules références autorisées sont les identifiants visibles dans SOURCES: [1], [2], ...
- Si tu cites, cite uniquement celles que tu as réellement utilisées.
- Si une seule source est pertinente, cite uniquement [1] (pas de "[1] à [4]").
- Ne cite pas de plage (ex: "[1]-[4]") si tu n'as pas utilisé toutes les sources de la plage.
- Si tu n'es pas sûr, ne cite pas et réponds : "Je ne sais pas.".

FORMAT DE RÉPONSE:
- Adapte la longueur et la structure de la réponse à la question posée.
- Pour une question factuelle simple, réponds directement, sans développement inutile ni répétition.
- Pour une demande d'analyse, d'explication, de comparaison, de synthèse ou de rapport, produis une
  réponse développée et structurée, proportionnée aux informations présentes dans les SOURCES.
- Ne réduis pas une réponse analytique à quelques puces quand plusieurs éléments pertinents sont
  disponibles.
- Ne complète jamais avec des informations absentes des SOURCES.
- Si les SOURCES sont insuffisantes, indique clairement ce qui manque.
- Puis, si utile, ajoute une ligne "Sources: [x], [y]".
```

> **Consigne de format ADAPTATIVE** (*implémenté, 30/07/2026*). Elle valait auparavant
> « Donne une réponse courte et directe », **quelle que soit la politique** — donc aussi sous les
> préambules `explain`, `compare` et `partial`, qui demandent l'inverse. Le prompt portait deux
> ordres contradictoires. Mesuré sur 249 réponses réelles (corpus de dev, même modèle) : médiane de
> **252 caractères** en politique `ok` contre **2 080** en `explain` ; un A/B contrôlé à budget de
> tokens **constant** donne ×2,3 sur cette seule ligne. La consigne délègue désormais la profondeur
> à la nature de la demande : elle n'autorise aucune extrapolation nouvelle et ne rend pas les
> réponses systématiquement longues — une question factuelle simple reste une réponse directe.
> Constantes `FORMAT_RULES_FR` / `FORMAT_RULES_EN` (`domain/retrieval.py`), verrouillées par
> `tests/tests_root/unit/query/test_prompt_format_policy.py`.
>
> **Taille surveillée.** Ce bloc est payé sur `RAG_MAX_PROMPT_CHARS`, donc **sur les
> sources** : sa première rédaction (688 c) retirait une source à 92 % des requêtes et
> vidait entièrement le prompt de deux questions sur cent. Il est condensé à **445 c**,
> sans rien retirer — les deux garde-fous « ne complète jamais hors SOURCES » et
> « indique ce qui manque » sont conservés au mot près, car ils ne sont écrits nulle part
> ailleurs. Le coût fixe total du gabarit est borné par un test
> (`overhead_ratio ≤ 0,18`, avertissement au-delà de 0,15).

### Templates intégrés en base (`prompt_templates.py`)

`PromptTemplate` par **workspace** (une seule active par `(workspace, name)`, versionnée). Deux
builtins prêts à seeder via `ensure_builtin_templates(workspace)` :

- `default` : factuel, concis, prudent.
- `explain` : structuré (contexte, points clés, impacts, incertitudes, synthèse).

Les garde-fous de citation sont **toujours injectés** en tête du prompt, même avec un template
custom.

---

## 4. Préambules conditionnels

Selon le `policy_label` renvoyé par le retrieval (voir [answerability.md](answerability.md)) :

| Label | Préambule | Setting |
|---|---|---|
| `low_confidence` | « réponds même si partiel + dis ce qui manque » | `RAG_LOWCONF_PROMPT_PREAMBLE` |
| `partial` | structure + zones d'incertitude | `RAG_PARTIAL_PROMPT_PREAMBLE` |
| `compare` | objets comparés, points communs, différences, conséquences, limites, synthèse | `RAG_COMPARE_PROMPT_PREAMBLE` |
| `explain` | structure + style pédagogique | `RAG_EXPLAIN_PROMPT_PREAMBLE` |
| `news` | priorise `document_date` | `RAG_NEWS_PROMPT_HINT` |

Ces préambules **précisent** la consigne de format du gabarit (§3) — ils ne la contredisent plus.
Le plan comparatif a été porté à sept sections le 30/07/2026 (ajout des *objets comparés* en tête
et des *conséquences / impacts* au milieu, synthèse déplacée en fin) : une comparaison réduite à
deux puces n'en est pas une. Le garde-fou reste identique — jamais de comparaison inventée quand
une seule entité est documentée. Voir [politique comparative](politique-comparative.md).

Autres prompts spécialisés : réécriture de requête (`query/rewrite.py`, « une seule ligne, 3-8
mots »), fallback Mode B (`chat_fallback.py`), résumé de conversation (`chat_threads.py` : n'invente
rien, n'inclut aucun secret, ne recopie pas verbatim).

---

## 5. Anti-hallucination

1. **Refus contrôlé** strict si `should_answer=False` **ET** `sources=[]` → message UI
   « Pas assez d'informations dans vos documents. », **aucun appel LLM**.
2. **Mode B bloqué** si `sources=[]` (évite l'hallucination) — le fallback hors-docs
   (`RAG_MODE_B_FALLBACK_ENABLED=0`, activable par flag) n'agit que si des sources existent mais
   sont insuffisantes.
3. **Garde-fous de citation** systématiquement en tête du prompt.
4. **Budget de prompt** `RAG_MAX_PROMPT_CHARS=18000` (évite les troncatures incohérentes ;
   déclaré et réglable — c'est lui qui limite le nombre de sources réellement transmises,
   cf. [../10-reference/variables-environnement.md](../10-reference/variables-environnement.md)).
5. **Heuristique « réponse simulée »** : réponse vide ou débutant par « réponse simulée » →
   `heuristic_answer` local, puis réapposition des sources.

### Politique « Je ne sais pas »

| Cas | Action |
|---|---|
| Aucun chunk après FTS+vector | `status=no_answer` → message fixe, aucun LLM |
| Chunks présents mais `should_answer=False` | mode `partial` : LLM appelé avec préambule explicite |
| `RAG_MODE_B_FALLBACK_ENABLED=1` + sources insuffisantes mais existantes | `fallback_general` avec disclaimer « ⚠️ Réponse hors documents du RagSpace. » |
| Erreur LLM | `status=error`, `error_stage=llm` |
| Erreur retrieval | `status=error`, `error_stage=retrieval` |

Détail du mécanisme de décision : [answerability.md](answerability.md).

---

## 6. Citations réellement émises (contrat de réponse)

**Statut : implémenté, activé par défaut.**

> **Correction de l'ancien comportement.** `finalize_answer_with_sources` réapposait une ligne
> `Sources: [1], [2]…` énumérant **toutes** les sources transmises au modèle — y compris sous
> une abstention (« Je ne sais pas. » + `Sources: [1]…[8]`, avec badge « 8 sources citées ✓ »
> côté UI). Ce n'était pas une citation : c'était le top-k reformaté en signal de confiance.

`apps/rag_core/services/answer_contract.py` est la source unique de vérité. Il distingue
**trois populations** et les expose dans le contrat `sources_contract` (réponse sync et
événement `done` du flux) :

| Champ | Sens |
|---|---|
| `retrieved_sources_count` | candidats **retrouvés** (trace `final_candidate_count`) |
| `prompt_sources_count` | sources **transmises** au modèle (après diversification + budget) |
| `cited_source_ids` / `cited_sources_count` | sources **réellement citées** dans la réponse |
| `answer_is_abstention` | la réponse est-elle une abstention ? |

Règles de finalisation :

| Cas | Ligne « Sources: » | Contrat |
|---|---|---|
| Abstention | **aucune** (celle du modèle est retirée) | `answer_is_abstention=true`, `cited_source_ids=[]` |
| Réponse citant `[a]`, `[b]` | `Sources: [a], [b]` — **jamais** le top-k complet | ids cités, hors-bornes écartés |
| Réponse sans citation | **aucune** | `cited_source_ids=[]` |
| `no_answer`, garde Excel | **aucune** | abstention forcée, sources visibles mais non citées |

La **détection d'abstention** (`is_abstention`) est centralisée, FR + EN, et ne repose pas sur
une égalité de chaîne : texte normalisé (casse, accents, apostrophes), formule reconnue
n'importe où, et mesure du texte **restant** — une réponse substantielle qui mentionne au
passage « les sources ne permettent pas de répondre sur ce point » n'est **pas** une abstention.
Le même détecteur écarte les refus de la mémoire de conversation (anti-anchoring).

Côté UI : badge vert « N sources citées » **uniquement** quand des citations existent ; sinon
« N sources consultées », sans marque de validation.

Un `answer_prefix` (avertissement de couverture) est appliqué **après** génération — le LLM ne
peut pas le retirer (idempotent).

---

## 7. Coût et audit

> **Correction de l'ancien doc.** Le coût **n'est plus** figé à `0.0`. Le routeur reste agnostique
> (`cost=None`), mais le coût **estimé** est calculé par `apps/rag_core/services/pricing.py`
> (`compute_cost_from_usage`) et stocké dans `RagQueryLog.llm_cost_estimated`. Le `0.0` ne concerne
> que les modèles locaux/ollama ; un modèle inconnu donne un coût `None`. Agrégations exposées via
> la CLI `llm_costs` et l'endpoint `audit/llm-costs`.

Chaque requête chat insère/maj un `RagQueryLog` (app `audit`) :

- `prompt_hash` (SHA-256 du prompt final, jamais le prompt),
- `llm_provider`, `llm_model`,
- `llm_prompt_tokens` / `llm_completion_tokens` / `llm_total_tokens` (best-effort selon provider),
- `llm_cost_estimated`, `latency_ms`, `policy` (label), `status` / `error_stage`,
- `sources` (snapshot versionné, payload `schema_version=3`), `request_id`,
- `feedback_score/reason/comment` (renseignés a posteriori via `/chat/feedback/`).

Minimisation question/réponse : `RAG_QUERY_LOG_STORE_QUESTION` / `RAG_QUERY_LOG_STORE_ANSWER`
(défaut 1). Rétention `RAG_QUERY_LOG_RETENTION_DAYS=90`. Gouvernance détaillée :
la documentation d'exploitation (hors périmètre de cette édition).

Génération obligatoirement via **AI Gateway** en env officiel (jamais Ollama direct) — voir
[ai-gateway.md](ai-gateway.md).

---

## Références d'implémentation

- `backend/apps/rag_core/domain/retrieval.py`
- `backend/apps/chat/services/prompt_templates.py`
- `backend/apps/chat/services/chat_fallback.py`
- `backend/apps/chat/services/chat_threads.py`
- `backend/apps/chat/services/intents/news.py`
- `backend/apps/rag_core/services/query/{heuristics,rewrite}.py`
- `backend/apps/rag_core/services/pricing.py`
- `backend/apps/audit/models.py` (`RagQueryLog`)
- `backend/config/settings/base.py`
