# Budget de prompt : mémoire contre sources

*Statut : **corrigé le 23/08/2026**.* Public : développeurs backend, exploitants.

Contexte du pipeline : [pipeline-retrieval.md](pipeline-retrieval.md).

---

## 1. Le défaut

`RAG_MAX_PROMPT_CHARS` plafonne le prompt **entier** : consignes, question et sources
puisent dans la même enveloppe. La mémoire de conversation était injectée **dans la
question**, avant l'allocation du budget. Elle entrait donc dans le coût réputé
**fixe**, et les sources étaient la seule variable d'ajustement.

Conséquence : plus une conversation s'allonge, moins l'assistant dispose de sources —
sans erreur, sans avertissement, sans trace.

### Mesuré, pas supposé

Sur 25 fils réels, le 23/08/2026 :

| | staging | production |
|---|---:|---:|
| mémoire, médiane | **3 045 c** | 1 359 c |
| mémoire, maximum | **6 686 c** | 5 426 c |
| sources encore possibles, médiane | **5** / 8 | 7 / 8 |
| sources encore possibles, minimum | **0** | 2 |

Un fil de staging tombait déjà à **zéro source**.

Et par construction, la mémoire peut atteindre **~7 592 caractères** —
`CHAT_MEMORY_SUMMARY_MAX_CHARS` (4 000) et `CHAT_MEMORY_MAX_CHARS` (3 500) sont deux
plafonds **qui s'additionnent** sans borne commune — contre un budget de prompt de
8 300. Soit 91 % de l'enveloppe pour la mémoire, dans le pire cas.

## 2. Pourquoi 8 300 n'était pas le coupable

La valeur a été **qualifiée** le 30/07/2026, pas héritée : gabarit 1 106 c + 8 sources
× 823 c = 7 690 c, avec 610 c de marge. Elle fait exactement ce pour quoi elle a été
calculée.

Ce qui manquait à cette arithmétique est un **terme pour la mémoire**. La marge prévue
(610 c) vaut le sixième de la mémoire médiane de staging.

## 3. Le correctif : inverser la priorité

```
RAG_MIN_PROMPT_SOURCES = 4
```

L'allocateur réserve la place de N sources **avant** d'allouer ce qui reste à la
mémoire, qui est tronquée par la fin si nécessaire.

> Les sources sont la substance d'un assistant documentaire : sans elles, plus de
> citation, plus de vérifiabilité. La mémoire est une aide à la résolution de
> références. **On peut répondre sans se souvenir ; on ne peut pas prouver sans
> source.**

Trois précisions qui comptent :

- **la troncature garde la FIN** de la mémoire — le plus récent, donc ce à quoi la
  question fait référence. C'est déjà la politique de `get_thread_memory_context` ;
- **le cadre de l'injection est compté**. Les consignes « uniquement pour résoudre les
  références… » coûtent des caractères ; les oublier faisait perdre une source de plus.
  Il est mesuré, pas estimé ;
- **`RAG_MIN_PROMPT_SOURCES=0` restaure exactement le comportement antérieur.** Le
  retour arrière est un réglage, et un test vérifie qu'il est réel.

## 4. L'éviction est désormais visible

La comptabilité du budget existait — coût fixe, place laissée aux sources, retraits,
troncature — et était exposée par l'API. Elle **ne survivait pas** dans
`RagQueryLog.retrieval` : une éviction était donc indétectable après coup.

C'est ce qui a rendu le diagnostic long : on lisait « 1 source » dans un journal sans
pouvoir dire ce qui l'avait réduite à une.

`business_corpus.prompt_budget` porte maintenant, sans aucun texte :

```
observed · dropped_workspace · dropped_corpus
prompt_overhead_chars · source_budget_chars · sources_dropped_by_budget
prompt_budget_exhausted · final_source_truncated
conversation_context_requested_chars · conversation_context_kept_chars
conversation_context_truncated · min_sources_reserved
```

Sous `RAG_PROMPT_RENDER_CONFORMANCE_V1_ENABLED=1` (défaut OFF), s'y ajoutent les compteurs
de **coût des règles conditionnelles** : `rules_kind`, `conditional_rules_chars`,
`source_context_chars`, `headers_deduplicated`, `prompt_total_chars`, `prompt_limit_chars`,
`prompt_remaining_chars`, `prompt_limit_exceeded`, `prompt_limit_exception_reason`
(vocabulaire fermé). Les règles CLASSEURS (2 812 c) et TABLEAUX (435 c) sont comptées
dans le prompt mais **pas** dans `prompt_overhead_chars` — ce dernier est mesuré sans
source, donc sans règle. Relation : `selected_source_chars = conditional_rules_chars +
source_context_chars`. Voir [pipeline-retrieval.md](pipeline-retrieval.md), section
« Conformité du rendu et observabilité du budget ».

> `RAG_MIN_PROMPT_SOURCES` **réserve** la place de N sources face à la mémoire ; il ne
> **garantit** pas N sources dans le prompt final. Un extrait de classeur de 4 000 c et ses
> règles saturent à eux seuls l'enveloppe : le prompt reste à une source.

## 5. Ce que ce lot ne corrige pas

**Les deux plafonds de mémoire restent indépendants du budget de prompt.** Le correctif
protège les sources en tronquant la mémoire, mais ne coordonne pas les enveloppes en
amont : `CHAT_MEMORY_*` continue de produire jusqu'à ~7 592 caractères que l'allocateur
jettera peut-être. C'est du travail inutile, pas une incohérence — et cela mérite son
propre lot.

---

## Références d'implémentation

- `backend/apps/rag_core/services/query/prompt_builder.py`
  (`_build_prompt_with_budget`, `_truncate_conversation_context`)
- `backend/apps/rag_core/services/query/trace.py` (`_prompt_budget_summary`)
- `backend/apps/chat/services/chat_threads.py` (`get_thread_memory_context`)
- `backend/config/registry/domains/retrieval.py` (`RAG_MIN_PROMPT_SOURCES`,
  `RAG_PROMPT_RENDER_CONFORMANCE_V1_ENABLED`)
- `backend/apps/rag_core/domain/retrieval.py` (`render_prompt`, comptabilité des règles)
- `tests/tests_root/unit/query/test_prompt_render_conformance.py`
- `tests/tests_root/unit/query/test_prompt_budget_source_floor.py`
