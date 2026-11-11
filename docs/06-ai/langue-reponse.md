# Langue de réponse IA

**Statut : implémenté et ACTIVÉ EN PRODUCTION le 28/07/2026**
(`RAG_ANSWER_LANGUAGE_POLICY_ENABLED=1`). P0 chat/fallback ·
P1 résumés/analyses/mémoire · P2 continuité, préférence persistée, sélecteur UI.
Une limite subsiste, côté MODÈLE et non côté produit : cf.
[Obéissance du modèle](#obéissance-du-modèle-à-la-directive--qwen330b-instruct).

La langue des **réponses générées** est distincte de la **langue d'interface**
(`preferred_locale`) et de la **langue des documents**. Elle est pilotée par une
préférence dédiée et un **résolveur commun unique**.

## Contrat

| Champ | Valeurs | Rôle |
|---|---|---|
| `preferred_locale` | `fr` \| `en` | Langue de l'**interface** (emails/erreurs). N'influence PAS la génération. |
| `preferred_answer_language` | `auto` \| `fr` \| `en` | Préférence **persistée** de langue de réponse (profil compte). |
| `answer_language` (requête) | `auto` \| `fr` \| `en` | Override optionnel par requête (chat). Un choix forcé `fr`/`en` l'emporte sur la préférence persistée. |

`preferred_answer_language` n'est **jamais** fusionné avec `preferred_locale` : ce sont
deux préférences indépendantes, exposées par `/auth/me/`, `/bootstrap/` et
`GET/PATCH /api/v1/me/preferences/`.

## Résolution (ordre)

1. préférence **forcée** `fr`/`en` (requête, sinon préférence persistée) ;
2. **détection de la dernière question** utilisateur ;
3. **langue précédente du thread** (continuité) si la question est indéterminable ;
4. `preferred_locale` en **dernier repli** — uniquement quand il n'y a ni question
   exploitable ni historique (ex. résumé de document déclenché hors conversation) ;
5. **français** (repli historique).

La locale d'interface n'est donc utilisée qu'en tout dernier recours, et jamais comme
synonyme de la préférence de réponse. Voir `apps/rag_core/services/answer_language.py`.
Une **seule** logique de détection existe (`detect_question_language`) ; la mémoire de
conversation et les suggestions de reformulation la réutilisent.

## Flag plateforme

`RAG_ANSWER_LANGUAGE_POLICY_ENABLED` (défaut `0`) — DISTINCT de la préférence
utilisateur :

- **désactivé** → prompts français historiques, **à l'octet près** (aucune régression) ;
- **activé + `auto`** → langue de la dernière question (puis continuité thread, puis fr) ;
- **activé + `fr|en`** → langue forcée.

> **Ce que « désactivé » signifie vraiment.** À `0`, `resolve_target()` renvoie `None`
> **pour toute entrée, y compris une préférence utilisateur explicitement réglée sur
> `en`**. Aucune directive de langue n'atteint le modèle : la plateforme répond
> systématiquement en français. Ce n'est pas un défaut d'obéissance du modèle — il ne
> reçoit jamais la consigne.

**Clé STATIQUE.** `runtime_editable=False`, `max_scope=None` : la clé n'est pas
surchargeable par l'Admin Core (`validate_scope` refuse tout override), et
`policy_enabled()` lit les settings statiques, jamais l'overlay. C'est délibéré — le
flag commute le comportement de TOUTE la plateforme, et un override par workspace ou
RagSpace produirait des fils mixtes FR/EN. Toute bascule exige donc une variable
d'environnement **et un redéploiement du backend ET des workers**.

Elle est lue via `reg()` : seules `0` et `1` sont acceptées. Une valeur ambiguë
(`true`, `yes`) **refuse le démarrage** au lieu de valoir silencieusement `False`.

### Incident de production (juillet 2026)

La clé était **absente de tous les fichiers d'environnement déployés** — production
comme staging. Elle valait donc `0` partout sauf en développement local (`.env`), où
elle vaut `1` : le comportement correct en dev masquait le défaut en production.
Symptôme observé : interface anglaise, question anglaise, réponse et résumés en
français. Deux causes indépendantes se cumulaient — ce flag, et un `language: "fr"`
forcé côté frontend sur les parcours de résumé. **Corriger l'une sans l'autre laisse la
moitié du défaut en place.** Les trois `.env.*.example` portent désormais la clé
explicitement, avec la valeur qui correspond à la doctrine de chaque environnement.

**Résolution.** Corrigé par les PR #69 (`ee0270f` politique + résumés, `85d8571` textes
déterministes du chat), qualifié en staging sur le modèle de production, puis **activé
en production le 28/07/2026**. Aucune migration, aucune purge de cache, aucune
régénération de résumé, aucune modification de profil : les deux comptes de production
étaient déjà en `auto`, et `variant_key` sépare nativement les variantes FR et EN.

## Matrice des chemins de génération

| Chemin | Localisé ? | Source de la langue |
|---|---|---|
| Chat RAG (base + ok/partial/low_confidence/explain) | ✅ FR/EN¹ | préférence + question + thread |
| Fallback hors sources (prompt + bandeau) | ✅ FR/EN | idem chat |
| *News* (réponse résumée) | ✅ FR/EN | idem chat |
| Narration tabulaire (déterministe) + clarification | ✅ FR/EN | cible résolue transportée au sous-chemin |
| Résumé document / page | ✅ FR/EN (cache séparé) | préférence explicite ; `auto` → `preferred_answer_language`, puis `preferred_locale` |
| Textes déterministes du résumé (« en cours », « échec ») | ✅ FR/EN | langue **concrète** du résumé (`resolve_summary_language`) |
| Abstention « Pas assez d'informations » | ✅ FR/EN | cible résolue (aucune génération) |
| Préambule de couverture partielle + bandeau `PARTIAL_COVERAGE_WARNING` | ✅ FR/EN | cible résolue, appliquée APRÈS génération |
| Erreurs de résumé affichées par le frontend | ✅ FR/EN | namespace i18n `errors` (locale d'interface) |
| Analyse globale (`statement`) | ✅ FR/EN | `parameters.answer_language` + question d'analyse |
| Résumé de conversation (mémoire) | ✅ FR/EN | détection du transcript (détecteur commun) |
| Suggestions de reformulation | ✅ (déjà) | détecteur **commun** (unifié) |
| **Titre de conversation** | ✅ *par nature* | dérivé du texte **de la question** (aucune génération, aucune langue imposée) |
| Citations, extraits, noms de fichiers, valeurs tabulaires | ❌ jamais traduits | données source |

¹ La cible est correctement résolue et injectée dans le prompt ; `qwen3:30b-instruct`
suit toutefois la langue de la question lorsqu'elle CONTREDIT une préférence forcée —
cf. [Obéissance du modèle](#obéissance-du-modèle-à-la-directive--qwen330b-instruct).

`params.language` de l'analyse = **langue de recherche** (FTS) ; `params.answer_language`
= **langue de réponse**. Sémantiques séparées (rétrocompat : `search_language` accepté
comme alias de `language`).

## Interface

`AnswerLanguageSelector` (chat) persiste `preferred_answer_language` via
`PATCH /api/v1/me/preferences/` (optimiste + rollback si échec). **Distinct** du
`LanguageSelector` d'interface, qui n'écrit que `preferred_locale` (cookie + profil) et
**ne modifie jamais** la langue de réponse.

Le sélecteur remonte sa valeur au chat via `onChange` ; le chat la joint à chaque
requête dans `answer_language`. Le champ est **omis** quand la préférence n'est pas
connue — le backend retombe alors sur la préférence persistée du profil. L'envoi
explicite rend un changement effectif **dès le message suivant**, sans attendre que le
PATCH de profil soit visible en base.

Les parcours de **résumé** n'envoient aucune langue : le backend résout d'après le
profil. Un `language: "fr"` en dur y a longtemps rendu tout résumé anglais impossible —
`api.ts` émet désormais `auto` par défaut, et le POST comme le polling GET utilisent
**strictement la même valeur** (le polling reprend même la langue concrète renvoyée par
le POST) pour viser la même variante de cache.

## Activation & rollback

- **Activer** : `RAG_ANSWER_LANGUAGE_POLICY_ENABLED=1` dans le fichier d'environnement,
  puis **redéployer le backend ET les workers** (la valeur est lue à l'import des
  settings ; un backend rechargé seul laisserait les tâches Celery sur l'ancien
  comportement). Aucune migration, aucune purge de cache, aucune régénération.
- **Rollback** : repasser à `0` et redéployer → retour au comportement français
  historique (prompts FR inchangés à l'octet près ; aucune donnée persistée n'a été
  modifiée). La migration `accounts.0003` (champ `preferred_answer_language`) est
  **réversible** et sans impact tant que le flag est `0`.
- **Résumés déjà en base** : rien à faire. `variant_key` inclut la langue
  (`bullets:document::fr` vs `…::en`) : une première demande anglaise crée une variante
  **indépendante** sans invalider ni écraser les variantes françaises existantes.
- **Préférences utilisateur** : aucune migration. Le défaut du champ est `auto`, et
  `auto` + question française reste `fr`.

### Critères GO staging → production

Le passage en production n'est autorisé que si **tous** ces points sont vérifiés :

1. ≥ 95 % des questions clairement anglaises produisent une réponse anglaise ;
2. les questions françaises restent en français (aucune régression) ;
3. une préférence explicite `en` est toujours respectée ;
4. une préférence explicite `fr` est toujours respectée ;
5. citations, extraits et noms de fichiers restent dans leur langue d'origine ;
6. résumés **synchrones et asynchrones (Celery)** respectent la langue demandée ;
7. le POST de résumé et son polling ciblent la même variante de cache ;
8. aucune régression de latence ni de fallback ;
9. le rollback (`=0` + redéploiement) a été **testé**, pas seulement décrit.

## Obéissance du modèle à la directive — `qwen3:30b-instruct`

**Statut : vérifié sur un scénario de référence synthétique** (9 appels, périmètre de test temporaire
et synthétique, supprimé depuis). Modèle servi via l'AI Gateway, `provider=ai_gateway`,
`model=qwen3:30b-instruct`.

> **`qwen3:30b-instruct` reproduit la langue de la QUESTION et ignore la directive de
> langue.** En l'absence de question — les résumés — il suit la directive fidèlement.

La propagation RAGatm est correcte dans tous les cas : `resolve_target()` rend la bonne
cible, `prompt_variant` la reflète, et `DIRECTIVE_EN` figure bien en tête du prompt. Ce
que le modèle produit peut néanmoins diverger.

| Cas | Cible résolue | Produit par le modèle | |
|---|---|---|---|
| `auto` + question EN | `en` | anglais | ✅ |
| `auto` + question FR | `fr` | français | ✅ |
| **`fr`/`en` forcé + question de la MÊME langue** | conforme | conforme | ✅ |
| **`en` forcé + question FR** | `en` | **français** | ❌ |
| **`fr` forcé + question EN** | `fr` | **anglais** | ❌ |
| Abstention (`Je ne sais pas.` / `I don't know.`) | conforme | conforme | ✅ |
| Résumé, préférence `en`, source mixte FR/EN | `en` | **anglais** | ✅ |
| Résumé, préférence `fr` | `fr` | français | ✅ |
| Bandeau de couverture partielle | conforme | conforme | ✅ |

### Ce que cela implique

- Le cas d'usage **dominant** (`auto`, valeur par défaut du champ) fonctionne dans les
  deux langues : c'est la question qui décide, et le modèle la suit.
- Les **résumés** respectent une préférence explicite, dans les deux sens — y compris
  pour produire un résumé anglais d'un document français.
- Seul échoue le scénario **« forcer, en chat, une langue contraire à celle de la
  question »**. Ce scénario échouait déjà avant l'activation de la politique : ce n'est
  pas une régression, c'est une limite non levée.
- Les textes **non générés** (abstentions, bandeau, préambule) ne dépendent pas du
  modèle et sont toujours conformes.

### Écart de conception à traiter si le cas devient bloquant

`directive_for()` ne rend une directive **que pour l'anglais** : le chemin français
repose sur un gabarit rédigé en français, sans consigne explicite. Forcer `fr` sur une
question anglaise n'envoie donc **aucune instruction de langue** — il n'y a rien à
ignorer, le produit ne demande rien. Deux pistes, non implémentées :

1. un `DIRECTIVE_FR` symétrique de `DIRECTIVE_EN` ;
2. la **répétition** de la directive en fin de prompt (les LLM pondèrent davantage la
   fin du contexte que son début).

Ces pistes ne sont pas nécessaires tant que `auto` reste la préférence dominante. Elles
le deviendraient si des utilisateurs réglaient massivement une langue de réponse fixe
tout en interrogeant dans une autre langue.

### Portée de la mesure

Neuf appels sur un corpus synthétique d'un seul document : suffisant pour établir la
**direction** du comportement (le modèle suit la question), pas pour en fournir un taux.
La mesure vaut pour `qwen3:30b-instruct` uniquement — un autre modèle servi par la
Gateway peut se comporter différemment et devra être requalifié.

## Limites connues

- **Questions courtes ou sans mot-outil** (« Risks? », « Budget », « 2025? ») : la
  détection renvoie `None`. La réponse suit alors la langue du **tour précédent**, puis
  le français. Un fil ouvert en français orientera donc une question anglaise courte
  vers le français — conséquence assumée de la règle de continuité.
- **Libellés de mode persistés** (« Réponse rapide », « Analyse complète ») : écrits
  dans le fil en français au moment du clic. Les traduire rétroactivement rendrait un
  même fil incohérent entre ses tours ; ils restent donc en français.
- **Routeur d'actions et suggestions de reformulation** : quelques textes déterministes
  restent français uniquement. Sans effet en production tant que
  `ACTIONS_ROUTER_ENABLED` et `RAG_REPHRASING_SUGGESTIONS_ENABLED` valent `0`.

> **Textes déterministes du chat — corrigés après qualification staging.** L'abstention
> « Pas assez d'informations… », le préambule de couverture partielle et le bandeau
> `PARTIAL_COVERAGE_WARNING` étaient codés en français uniquement. Ces textes ne sont
> **pas générés** : aucune directive de langue ne s'y applique, et ils apparaissaient
> donc en français sous une réponse anglaise — cible pourtant correctement résolue
> (`resolved_answer_language=en`, `prompt_variant=en` dans le journal). Le défaut n'était
> visible ni en revue de code ni en tests unitaires : il a fallu exécuter la matrice
> contre un vrai modèle pour qu'il apparaisse. Ils suivent désormais la langue résolue.

## Observabilité

`log_language_decision()` (dans `answer_language.py`) émet un événement structuré
`answer_language_decision` sur chaque surface (`chat.sync`, `chat.stream`, `summary`) :

`policy_enabled`, `requested_answer_language`, `question_len`,
`detected_question_language`, `thread_prev_language`, `ui_locale`,
`resolved_answer_language`, `prompt_variant`, `provider`, `model` — plus `variant_key`
pour les résumés.

`prompt_variant=legacy_fr` signale que la politique est **désactivée** et qu'aucune
directive de langue n'a été envoyée : c'est la signature exacte de l'incident ci-dessus.

Le journal ne contient **ni la question, ni le prompt, ni le contenu documentaire**
(seule la *longueur* de la question est relevée, pour distinguer « trop courte pour être
détectée » d'une détection en échec). `RagQueryLog` ne porte aucun champ de langue :
l'ajouter exigerait une migration et n'a pas été fait.

## Qualification (avant activation)

Deux outils **ré-exécutables** préparés (ils n'activent rien) — voir
`scripts/qualif/README.md` :
- **Invariance retrieval** : `scripts/qualif/bench_answer_language_invariance.sh` rejoue le
  benchmark retrieval-only (flag OFF vs ON) et compare les sorties canoniques (identiques
  attendues) + byte-identité du prompt FR. Aucun holdout consommé.
- **E2E providers réels** : `python manage.py qualify_answer_language --live` exécute la
  matrice (auto/forcé/continuité/fallback/résumé/analyse) avec les vrais providers ;
  rapport PASS/FAIL par cas, sans journaliser documents/prompts/secrets.

## Références d'implémentation

- `backend/apps/rag_core/services/answer_language.py` — résolveur (chaîne complète), détection, flag, directive EN.
- `backend/apps/rag_core/services/query/prompt_builder.py`, `domain/retrieval.py` — chat (préambules + base) FR/EN.
- `backend/apps/chat/services/chat_service.py` — résolution (préférence persistée + thread) + threading sync/SSE.
- `backend/apps/chat/services/chat_threads.py` — `thread_previous_user_language`, mémoire FR/EN.
- `backend/apps/chat/services/chat_fallback.py`, `intents/news.py`, `tabular_routing.py` — fallback / news / tabulaire FR/EN.
- `backend/apps/rag_core/services/summary_service.py`, `analysis/global_search.py` — résumés / analyses FR/EN.
- `backend/apps/chat/services/rephrasing_suggestions.py` — détection **unifiée**.
- `backend/apps/accounts/models.py` (`preferred_answer_language` + migration 0003), `api/v1/me/`, `api/v1/analyses/serializers.py`.
- `frontend/src/components/AnswerLanguageSelector.tsx` — sélecteur UI persistant (`onChange` → chat).
- `frontend/src/app/chat/_lib/chatRequest.ts` — `answer_language` dans le corps de requête.
- `frontend/src/lib/api.ts` — `summaryErrorKey()` (clés i18n) ; défauts `auto` POST/polling.
- `backend/config/settings/base.py` (lecture via `reg()`), `backend/config/registry/domains/retrieval.py` (clé statique), `backend/config/settings/from_registry.py` (booléen strict `0`/`1`).
- `backend/apps/chat/services/summary_messages.py`, `backend/apps/actions/wiring.py` — textes déterministes FR/EN.
- `tests/tests_root/unit/query/test_answer_language.py`, `tests/tests_root/integration/test_answer_language_p1.py`, `..._p2.py`, `tests/tests_root/http_api/test_documents_summary_language.py` ; `tests/frontend/i18n/AnswerLanguageSelector.test.tsx`, `tests/frontend/lib/chatRequest.test.ts`, `tests/frontend/lib/api.test.ts`.
