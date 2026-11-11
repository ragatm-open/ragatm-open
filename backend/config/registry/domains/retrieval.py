"""Domaine ``retrieval`` — recherche, fusion, classement et budgets de contexte."""
from __future__ import annotations

from config.registry.definition import ConfigDefinition as D

DEFINITIONS: tuple[D, ...] = (
    # --- Politique de réponse ------------------------------------------------
    D(
        name="RAG_MIN_SOURCES",
        domain="retrieval",
        value_type="int",
        default=1,
        minimum=0,
        description="Nombre minimal de sources retenues pour accepter de répondre.",
        consumers=("apps.rag_core.domain.policies",),
        notes=(
            "Également relu depuis l'environnement par `domain/policies.py` "
            "(même défaut) : voir la section « doublons » de la référence."
        ),
    ),
    D(
        name="RAG_MIN_TEXT_RANK",
        domain="retrieval",
        value_type="float",
        default=0.0,
        minimum=0.0,
        maximum=1.0,
        description="Rang lexical (FTS) minimal d'un passage pour être considéré pertinent.",
        consumers=("apps.rag_core.domain.policies",),
    ),
    # ------------------------------------------------------------------
    # Politique d'ADMISSION du retrieval (lot 4)
    # ------------------------------------------------------------------
    # `should_answer` décidait par un seuil absolu, en booléen, et avec un ordre qui
    # a une conséquence rarement énoncée : dès qu'un signal FTS existe, RAG_MIN_SCORE
    # n'est plus appliqué. En mode hybride — la configuration réelle des deux
    # environnements — la décision se réduit donc à `ts_rank >= RAG_MIN_TEXT_RANK`.
    #
    # Mesuré au lot 3 : les scores des positifs et des négatifs se RECOUVRENT. Un
    # seuil absolu seul ne peut donc pas les séparer, où qu'on le place. C'est la
    # SÉPARATION entre le premier et le deuxième résultat qui discrimine.
    D(
        name="RAG_ADMISSION_POLICY",
        domain="retrieval",
        value_type="str",
        default="legacy",
        description=(
            "Politique d'admission du retrieval : `legacy` (historique, à l'octet "
            "près), `score_only` (seuil absolu sur les DEUX modalités), "
            "`score_and_margin` (seuil + séparation top-1/top-2), `tiered` (seuil + "
            "marge + zone UNCERTAIN explicite)."
        ),
        consumers=(
            "apps.rag_core.domain.retrieval_admission",
            "apps.rag_core.services.query.orchestrator",
        ),
        notes=(
            "Livré à `legacy` : l'activation est un acte de déploiement mesuré, et "
            "c'est aussi l'interrupteur de rollback (aucune migration, aucune "
            "réindexation)."
        ),
    ),
    D(
        name="RAG_MIN_MARGIN",
        domain="retrieval",
        value_type="float",
        default=0.0,
        minimum=0.0,
        maximum=1.0,
        description=(
            "Séparation minimale entre le 1er et le 2e score VECTORIEL. Calculée "
            "entre scores de même nature uniquement — comparer une marge vectorielle "
            "à un `ts_rank` n'aurait aucun sens. 0.0 = marge inactive."
        ),
        consumers=("apps.rag_core.domain.retrieval_admission",),
        notes=(
            "Un bon score sans séparation est la signature d'un corpus qui ne "
            "contient PAS la réponse : une grappe de passages également médiocres."
        ),
    ),
    D(
        name="RAG_UNCERTAIN_SCORE",
        domain="retrieval",
        value_type="float",
        default=0.0,
        minimum=0.0,
        maximum=1.0,
        description=(
            "Plancher de la zone d'incertitude (`tiered`). Sous ce score, le résultat "
            "est REJETÉ ; entre lui et RAG_MIN_SCORE, il est UNCERTAIN. DOIT être "
            "<= RAG_MIN_SCORE : la zone d'incertitude vit SOUS le seuil d'acceptation."
        ),
        consumers=("apps.rag_core.domain.retrieval_admission",),
        notes="UNCERTAIN n'est JAMAIS compté comme une acceptation dans les métriques.",
    ),
    D(
        name="RAG_TEXT_ADMISSION_POLICY",
        domain="retrieval",
        value_type="str",
        default="independent",
        description=(
            "Rôle du signal lexical dans l'admission : `independent` (un ts_rank "
            "suffisant admet seul — comportement historique), `combined` (le lexical "
            "CORROBORE, il ne décide pas seul face à un vectoriel faible), `disabled` "
            "(le lexical n'entre pas dans l'admission)."
        ),
        consumers=("apps.rag_core.domain.retrieval_admission",),
        notes=(
            "`combined` corrige le contournement : un ts_rank élevé signale une "
            "correspondance de MOTS, pas de sens."
        ),
    ),
    D(
        name="RAG_MIN_SCORE",
        domain="retrieval",
        value_type="float",
        default=0.76,
        minimum=0.0,
        maximum=1.0,
        description=(
            "Seuil de similarité vectorielle, calibré pour le profil de référence "
            "ollama/nomic-embed-text (dim 768). Appliqué sur le chemin VECTORIEL-SEUL."
        ),
        consumers=("apps.rag_core.domain.policies",),
        notes="Propriété du couple modèle × corpus × fusion : ne pas réutiliser tel quel.",
    ),
    D(
        name="RAG_TOP_K",
        domain="retrieval",
        value_type="int",
        default=8,
        minimum=1,
        maximum=100,
        description="Nombre de passages transmis au prompt après fusion et troncature.",
        consumers=("apps.rag_core.services.query", "apps.rag_core.domain.policies"),
        notes=(
            "DOUBLON connu : `domain/policies.py` relit la variable avec un défaut de 6 "
            "(et non 8). Divergence historique conservée en l'état par ce lot."
        ),
    ),
    D(
        name="RAG_VECTOR_TOP_K",
        domain="retrieval",
        value_type="int",
        default=8,
        minimum=1,
        maximum=200,
        description="Nombre de candidats rapportés par le leg vectoriel avant fusion.",
        consumers=("apps.rag_core.services.query", "apps.rag_core.domain.policies"),
        notes="DOUBLON connu : défaut 40 dans `domain/policies.py`.",
    ),
    D(
        name="RAG_HYBRID_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=True,
        description=(
            "Recherche hybride (lexical + vectoriel). Source de vérité unique, parsing "
            "booléen robuste : un `== \"1\"` naïf désactivait le vectoriel en silence."
        ),
        consumers=("apps.rag_core.services.query",),
    ),
    D(
        name="RAG_FTS_CONFIG",
        domain="retrieval",
        value_type="enum",
        choices=("simple", "french", "english"),
        default="simple",
        description=(
            "Configuration FTS PostgreSQL. `simple` est robuste sur sigles et corpus "
            "bilingue ; `french` ajoute stemming et stop words (index GIN dédié)."
        ),
        consumers=("apps.rag_core.services.query",),
    ),
    D(
        name="RAG_FTS_STORED_VECTOR_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=False,
        description=(
            "Lit le vecteur lexical dans la colonne générée `text_tsv_french` "
            "(index GIN) au lieu de recalculer `to_tsvector` à chaque requête. "
            "Sémantique de classement IDENTIQUE : la colonne matérialise la même "
            "expression. Exige RAG_FTS_CONFIG='french' et le schéma migré "
            "(rag_core.0039/0040, corpus.0006/0007) — refus au démarrage sinon."
        ),
        restart_required=True,
        consumers=(
            "apps.rag_core.repositories.chunk_repository",
            "apps.corpus.services.retrieval",
        ),
        notes=(
            "La config FTS est figée dans le schéma par la colonne générée : "
            "changer RAG_FTS_CONFIG impose de refaire la migration."
        ),
    ),
    D(
        name="RAG_HNSW_EF_SEARCH",
        domain="retrieval",
        value_type="int",
        default=40,
        minimum=1,
        maximum=1000,
        description=(
            "Paramètre HNSW de pgvector : rappel contre latence de la recherche "
            "vectorielle (défaut pgvector = 40)."
        ),
        consumers=("apps.rag_core.repositories",),
    ),
    # --- Diversité et fusion -------------------------------------------------
    D(
        name="RAG_RETRIEVAL_DIVERSITY_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=True,
        description=(
            "Récupère un pool élargi (candidate_k), diversifie par groupe puis tronque "
            "au top_k."
        ),
        consumers=("apps.rag_core.services.query",),
    ),
    D(
        name="RAG_RETRIEVAL_CANDIDATE_MULTIPLIER",
        domain="retrieval",
        value_type="int",
        default=3,
        minimum=1,
        maximum=20,
        description="Multiplicateur du pool de candidats quand candidate_k n'est pas fixé.",
        consumers=("apps.rag_core.services.query",),
    ),
    D(
        name="RAG_RETRIEVAL_CANDIDATE_K",
        domain="retrieval",
        value_type="int",
        default=0,
        minimum=0,
        maximum=1000,
        description="Taille absolue du pool de candidats. 0 => dérivée du multiplicateur.",
        consumers=("apps.rag_core.services.query",),
    ),
    D(
        name="RAG_RETRIEVAL_MAX_PER_GROUP",
        domain="retrieval",
        value_type="int",
        default=2,
        minimum=1,
        maximum=50,
        description="Nombre maximal de passages conservés par groupe (document/feuille).",
        consumers=("apps.rag_core.services.query",),
    ),
    D(
        name="RAG_RRF_CONDITIONAL_LEXICAL_WEIGHT_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=True,
        description=(
            "Renforce la contribution lexicale de la fusion RRF quand la question porte "
            "un identifiant fort. Sans identifiant : poids (1, 1), fusion inchangée."
        ),
        consumers=("apps.rag_core.services.query",),
    ),
    D(
        name="RAG_RRF_STRONG_IDENTIFIER_LEXICAL_WEIGHT",
        domain="retrieval",
        value_type="float",
        default=1.5,
        minimum=1.0,
        maximum=10.0,
        description="Poids lexical appliqué en présence d'un identifiant fort.",
        consumers=("apps.rag_core.services.query",),
    ),
    D(
        name="RAG_FTS_SLASH_IDENTIFIERS_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=False,
        description=(
            "Ajoute une requête FTS littérale pour les références à slash "
            "(`FIN01/FIN01-01`), que le plan historique décompose en termes inexistants."
        ),
        consumers=("apps.rag_core.services.query",),
    ),
    D(
        name="RAG_FTS_QUERY_NORMALIZATION_V2",
        domain="retrieval",
        value_type="bool",
        default=False,
        description=(
            "Construction de requête lexicale normalisée (V2). Le plan se réduit à UNE "
            "disjonction des termes porteurs de la question, mots vides anglais retirés, "
            "tirets et apostrophes conservés. Corrige un défaut mesuré : la configuration "
            "FTS `french` ne reconnaît pas `what`/`does`/`the`, l'entrée sélective du plan "
            "historique se réduisait à `what:*` et ramenait des résultats non vides — le "
            "repli, conditionné à zéro résultat, n'était donc jamais atteint."
        ),
        consumers=(
            "apps.rag_core.repositories.chunk_repository",
            "apps.corpus.services.retrieval",
        ),
        notes=(
            "Compromis MESURÉ et assumé : qualité en hausse (+8 preuves au contexte sur le "
            "benchmark gelé), médiane FTS ×1,50, p95 ×0,74. Le repli de plan disparaît par "
            "construction. Retour arrière = repasser à 0, sans réindexation ni migration."
        ),
    ),
    D(
        name="RAG_STRONG_SIGNALS_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=False,
        description=(
            "Signaux forts d'une question (dates, valeurs, opérateurs temporels). Livré "
            "INACTIF : gain mesuré mais seuils de qualification non atteints."
        ),
        consumers=("apps.rag_core.services.query",),
    ),
    D(
        name="RAG_TEMPORAL_CANDIDATES_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=False,
        description=(
            "Leg documentaire borné sur les métadonnées de date, pour les questions "
            "temporelles globales. Strictement conditionnel à un opérateur temporel."
        ),
        consumers=("apps.rag_core.services.query.temporal_candidates",),
    ),
    D(
        name="RAG_TEMPORAL_CANDIDATE_LIMIT",
        domain="retrieval",
        value_type="int",
        default=100,
        minimum=1,
        maximum=1000,
        description=(
            "Borne ABSOLUE du leg temporel : nombre maximal de documents rapportés par "
            "la requête de métadonnées."
        ),
        consumers=("apps.rag_core.services.query.temporal_candidates",),
    ),
    D(
        name="RAG_SPACE_MIN_SCORE_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=False,
        description=(
            "Autorise `RagSpace.min_score` à primer sur le seuil global. Ne baisse rien : "
            "un espace sans valeur explicite se comporte exactement comme avant."
        ),
        consumers=("apps.rag_core.domain.policies",),
    ),
    D(
        name="RAG_WORKSPACE_SOURCE_BOOST",
        domain="retrieval",
        value_type="float",
        default=1.10,
        minimum=0.1,
        maximum=10.0,
        description=(
            "Boost multiplicatif des documents PRIVÉS du workspace face au socle corpus "
            "partagé. 1.0 = neutre."
        ),
        consumers=("apps.rag_core.services.query",),
    ),
    D(
        name="RAG_DOMAIN_CORPUS_SOURCE_BOOST",
        domain="retrieval",
        value_type="float",
        default=1.00,
        minimum=0.1,
        maximum=10.0,
        description="Boost multiplicatif du socle corpus métier partagé. 1.0 = neutre.",
        consumers=("apps.rag_core.services.query",),
    ),
    D(
        name="RAG_FTS_PLAN_MAX_ATTEMPTS",
        domain="retrieval",
        value_type="int",
        default=6,
        minimum=1,
        maximum=20,
        description=(
            "Nombre maximal de requêtes FTS exécutées pour une même question (repli de "
            "plan). Borne partagée par le leg privé et le leg corpus métier."
        ),
        consumers=("apps.rag_core.domain.fts_plan",),
        notes=(
            "Le plan le plus long que produit `build_tsquery_plan` compte 6 éléments : "
            "le défaut ne tronque aucun plan réel, il borne le coût d'une dérive future."
        ),
    ),
    D(
        name="RAG_BUSINESS_CORPUS_RRF_LEG_WEIGHT",
        domain="retrieval",
        value_type="float",
        default=1.0,
        minimum=0.01,
        maximum=1.0,
        description=(
            "Poids des legs corpus métier dans la fusion de rangs unifiée. Les legs "
            "privés votent à 1.0 ; α ∈ ]0, 1] atténue le vote du corpus."
        ),
        consumers=("apps.rag_core.services.query.candidate_retriever",),
        notes=(
            "Sans effet si `RAG_BUSINESS_CORPUS_UNIFIED_FUSION_ENABLED=0`. α = 1.0 "
            "reproduit la fusion unifiée non pondérée. Ce n'est PAS un quota : aucune "
            "place n'est réservée, seul le poids du vote de rang change. Une valeur "
            "hors ]0, 1] est REFUSÉE au démarrage, jamais ramenée silencieusement à 1.0."
        ),
    ),
    D(
        name="RAG_MIN_FINAL_SOURCE_CHARS",
        domain="retrieval",
        value_type="int",
        default=400,
        minimum=1,
        maximum=4000,
        description=(
            "Taille minimale de l'extrait de la dernière source conservée quand le "
            "budget de prompt est saturé. Le budget tronque cette source, il ne la "
            "supprime plus."
        ),
        consumers=("apps.rag_core.services.query.prompt_builder",),
        notes=(
            "Garantit l'invariant « au moins une source ». Si même cet extrait minimal "
            "ne tient pas dans `RAG_MAX_PROMPT_CHARS`, la source est CONSERVÉE malgré le "
            "dépassement et l'état est tracé "
            "(`prompt_budget_unable_to_fit_minimum_source`) : une réponse sans preuve "
            "est plus grave qu'un prompt de quelques centaines de caractères trop long."
        ),
    ),
    D(
        name="RAG_BUSINESS_CORPUS_EMBEDDING_FINGERPRINT_ENFORCEMENT_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=False,
        description=(
            "Filtrage vectoriel des corpus métier par empreinte d'indexation : seuls "
            "les documents encodés avec l'identité courante du corpus sont comparés."
        ),
        consumers=("apps.corpus.services.retrieval",),
        notes=(
            "OFF : empreintes calculées et tracées, aucune exclusion (comportement "
            "historique). ON : filtre appliqué EN SQL. À la sortie de la migration "
            "aucun document ne porte d'empreinte — activer avant "
            "`backfill_corpus_embedding_fingerprints` viderait le leg vectoriel. "
            "Le leg lexical n'est jamais filtré par ce réglage."
        ),
    ),
    D(
        name="RAG_BUSINESS_CORPUS_CALIBRATED_ADMISSION_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=False,
        description=(
            "Porte d'admission calibrée des corpus métier : un corpus ne participe à la "
            "fusion que si la probabilité calibrée de sa pertinence atteint le seuil."
        ),
        consumers=(
            "apps.rag_core.services.query.candidate_retriever",
            "apps.corpus.services.admission",
        ),
        notes=(
            "Mécanisme DISTINCT de `RAG_BUSINESS_CORPUS_RRF_LEG_WEIGHT` : l'admission "
            "décide de la participation, α du poids du vote une fois admis. Décision "
            "TOUT-OU-RIEN par corpus et par question, portant sur les DEUX legs. "
            "Sans calibration active et valide, le corpus est REJETÉ explicitement — "
            "jamais admis en silence. Le signal reposant sur le leg vectoriel, activer "
            "cette porte avec `RAG_HYBRID_ENABLED=0` retire le corpus de la fusion."
        ),
    ),
    D(
        name="RAG_BUSINESS_CORPUS_ADMISSION_PROBABILITY_THRESHOLD",
        domain="retrieval",
        value_type="float",
        default=0.65,
        minimum=0.0,
        maximum=1.0,
        description=(
            "Seuil sur la PROBABILITÉ calibrée d'admission d'un corpus métier "
            "(`p >= seuil` admet)."
        ),
        consumers=("apps.rag_core.services.query.candidate_retriever",),
        notes=(
            "Porte sur une PROBABILITÉ, jamais sur le score brut du signal : le seuil "
            "brut observé (≈ 0,645 sur `corpus_vector.top10_mean` pour le corpus de "
            "référence) n'est pas transposable d'un corpus ni d'un modèle d'embeddings "
            "à l'autre. Politique globale ; la calibration reste propre à chaque corpus. "
            "Sans effet si la porte est désactivée."
        ),
    ),
    D(
        name="RAG_BUSINESS_CORPUS_UNIFIED_FUSION_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=False,
        description=(
            "Fusion bi-source unifiée : les candidats des corpus métier votent dans la "
            "même fusion de rangs (RRF) que les candidats privés."
        ),
        consumers=("apps.rag_core.services.query.candidate_retriever",),
        notes=(
            "OFF => assemblage historique : le corpus est ajouté APRÈS la fusion, donc "
            "sans `fusion_score`, et la diversification le classe derrière tout candidat "
            "privé quelle que soit sa pertinence. Activation par environnement après "
            "qualification benchmark. Rollback immédiat : repasser à 0 (aucune migration, "
            "aucune réindexation)."
        ),
    ),
    D(
        name="RAG_MODE_B_FALLBACK_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=False,
        description=(
            "Mode B : en l'absence de source pertinente, autorise une réponse LLM "
            "générale (hors corpus)."
        ),
        consumers=("apps.rag_core.services.query",),
    ),
    D(
        name="RAG_ANSWER_LANGUAGE_POLICY_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=False,
        description=(
            "Politique de langue de réponse (plateforme). OFF => prompts français "
            "historiques inchangés à l'octet près. N'affecte ni le retrieval ni les citations."
        ),
        consumers=("apps.rag_core.services.answer_language",),
        notes=(
            "Clé STATIQUE et volontairement NON surchargeable (`runtime_editable=False`) : "
            "elle commute le comportement de TOUTE la plateforme, un override par "
            "workspace ou RagSpace produirait des fils mixtes FR/EN incohérents. "
            "Activation = variable d'environnement + redéploiement du backend ET des "
            "workers ; `policy_enabled()` lit les settings statiques, pas l'overlay Core."
        ),
    ),
    # --- Bruit structurel ----------------------------------------------------
    D(
        name="RAG_STRUCTURAL_NOISE_PENALTY_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=False,
        description=(
            "Dévalorise les passages structurellement peu affirmatifs (questions, titres). "
            "Livré DÉSACTIVÉ ; rollback immédiat sans réindexation."
        ),
        consumers=("apps.rag_core.services.query",),
    ),
    D(
        name="RAG_STRUCTURAL_NOISE_PENALTY_FACTOR",
        domain="retrieval",
        value_type="float",
        default=0.75,
        minimum=0.0,
        maximum=1.0,
        description="Facteur de pénalité appliqué au score. 1.00 = neutre.",
        consumers=("apps.rag_core.checks_structural_noise",),
    ),
    # --- Budgets analytiques -------------------------------------------------
    D(
        name="RAG_EXPLAIN_TOP_K",
        domain="retrieval",
        value_type="int",
        default=8,
        default_expr="= RAG_TOP_K (8)",
        minimum=1,
        maximum=100,
        description="Budget top_k du mode explicatif. Défaut = budget standard.",
        consumers=("apps.rag_core.services.query",),
        default_is_dynamic=True,
    ),
    D(
        name="RAG_EXPLAIN_VECTOR_TOP_K",
        domain="retrieval",
        value_type="int",
        default=8,
        default_expr="= RAG_VECTOR_TOP_K (8)",
        minimum=1,
        maximum=200,
        description="Budget vectoriel du mode explicatif. Défaut = budget standard.",
        consumers=("apps.rag_core.services.query",),
        default_is_dynamic=True,
    ),
    D(
        name="RAG_EXPLAIN_MIN_TEXT_RANK",
        domain="retrieval",
        value_type="float",
        default=0.0,
        default_expr="= RAG_MIN_TEXT_RANK (0.0)",
        minimum=0.0,
        maximum=1.0,
        description="Rang lexical minimal du mode explicatif. Défaut = seuil standard.",
        consumers=("apps.rag_core.services.query",),
        default_is_dynamic=True,
    ),
    D(
        name="RAG_EXPLAIN_RERANK_TRIGGER_MIN_CANDIDATES",
        domain="retrieval",
        value_type="int",
        default=6,
        default_expr="= RAG_RERANK_TRIGGER_MIN_CANDIDATES (6)",
        minimum=0,
        maximum=200,
        description="Seuil de déclenchement du reranking en mode explicatif.",
        consumers=("apps.rag_core.services.query",),
        default_is_dynamic=True,
    ),
    D(
        name="RAG_EXPLAIN_MIN_SCORE",
        domain="retrieval",
        value_type="float",
        default=None,
        default_expr="(vide) — optionnel",
        minimum=0.0,
        maximum=1.0,
        description=(
            "Seuil vectoriel OPTIONNEL du mode explicatif. Vide => aucun plafonnement : "
            "le figer plafonnerait le seuil des espaces qui en portent un plus élevé."
        ),
        consumers=("apps.rag_core.services.query",),
        default_is_dynamic=True,
    ),
    # --- Budgets de surcharge par mode ---------------------------------------
    #
    # Ces douze réglages étaient lus par `getattr(settings, nom, défaut)` SANS
    # jamais être projetés : les poser dans un `.env` n'avait aucun effet. Ils
    # sont désormais déclarés, projetés et lus par `get_registered_setting`.
    #
    # Sémantique COMMUNE des surcharges de budget : `max(budget_courant, surcharge)`.
    # Un budget de mode ne peut donc qu'ÉLARGIR — d'où le défaut `0`, qui vaut
    # « aucune surcharge » et reproduit exactement le comportement d'avant.
    D(
        name="RAG_INVENTORY_TOP_K",
        domain="retrieval",
        value_type="int",
        default=0,
        default_expr="0 = aucune surcharge (budget standard conservé)",
        minimum=0,
        maximum=200,
        description=(
            "Surcharge du budget top_k en mode inventaire (« liste les documents… »). "
            "Ne peut qu'élargir : une valeur inférieure à RAG_TOP_K est sans effet."
        ),
        consumers=("apps.rag_core.services.query.orchestrator",),
    ),
    D(
        name="RAG_INVENTORY_VECTOR_TOP_K",
        domain="retrieval",
        value_type="int",
        default=0,
        default_expr="0 = aucune surcharge",
        minimum=0,
        maximum=400,
        description="Surcharge du budget vectoriel en mode inventaire. Ne peut qu'élargir.",
        consumers=("apps.rag_core.services.query.orchestrator",),
    ),
    D(
        name="RAG_INVENTORY_MIN_UNIQUE_DOCS",
        domain="retrieval",
        value_type="int",
        default=0,
        default_expr="0 = borne calculée (min(10, max(4, top_k)))",
        minimum=0,
        maximum=100,
        description=(
            "Nombre minimal de documents DISTINCTS visés en mode inventaire. À 0, la "
            "borne est calculée depuis le budget courant plutôt que figée."
        ),
        consumers=("apps.rag_core.services.query.orchestrator",),
    ),
    D(
        name="RAG_RERANK_POOL_K",
        domain="retrieval",
        value_type="int",
        default=20,
        minimum=1,
        maximum=400,
        description=(
            "Taille du pool soumis au reranker. Le pool est au moins égal au top_k "
            "final : reclasser moins d'extraits qu'on n'en rend n'aurait pas de sens."
        ),
        consumers=("apps.rag_core.services.query.candidate_reranker",),
    ),
    D(
        name="RAG_INVENTORY_RERANK_POOL_K",
        domain="retrieval",
        value_type="int",
        default=0,
        default_expr="0 = aucune surcharge",
        minimum=0,
        maximum=400,
        description="Surcharge du pool de reranking en mode inventaire. Ne peut qu'élargir.",
        consumers=("apps.rag_core.services.query.candidate_reranker",),
    ),
    D(
        name="RAG_INVENTORY_RERANK_TAKE",
        domain="retrieval",
        value_type="int",
        default=0,
        default_expr="0 = aucune surcharge",
        minimum=0,
        maximum=200,
        description="Surcharge du nombre d'extraits retenus après reranking, mode inventaire.",
        consumers=("apps.rag_core.services.query.candidate_reranker",),
    ),
    D(
        name="RAG_EXPLAIN_RERANK_POOL_K",
        domain="retrieval",
        value_type="int",
        default=0,
        default_expr="0 = aucune surcharge",
        minimum=0,
        maximum=400,
        description="Surcharge du pool de reranking en mode explicatif. Ne peut qu'élargir.",
        consumers=("apps.rag_core.services.query.candidate_reranker",),
    ),
    D(
        name="RAG_EXPLAIN_RERANK_TAKE",
        domain="retrieval",
        value_type="int",
        default=0,
        default_expr="0 = aucune surcharge",
        minimum=0,
        maximum=200,
        description="Surcharge du nombre d'extraits retenus après reranking, mode explicatif.",
        consumers=("apps.rag_core.services.query.candidate_reranker",),
    ),
    # --- Réponse à faible confiance ------------------------------------------
    D(
        name="RAG_LOWCONF_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=True,
        description=(
            "Signale une réponse construite sur des sources faibles au lieu de la "
            "rendre comme les autres. Éteindre ce garde-fou ne rend pas les réponses "
            "meilleures : il les rend indiscernables."
        ),
        consumers=("apps.rag_core.services.query.lowconf",),
    ),
    D(
        name="RAG_LOWCONF_FACTOR",
        domain="retrieval",
        value_type="float",
        default=1.0,
        minimum=0.0,
        maximum=10.0,
        description=(
            "Facteur appliqué au seuil de référence pour qualifier une réponse de "
            "faible confiance. >1 élargit le signalement, <1 le restreint."
        ),
        consumers=("apps.rag_core.services.query.lowconf",),
    ),
    D(
        name="RAG_LOWCONF_MIN_ABS",
        domain="retrieval",
        value_type="float",
        default=0.0,
        minimum=0.0,
        maximum=1.0,
        description="Plancher absolu de score sous lequel la confiance est réputée faible.",
        consumers=("apps.rag_core.services.query.lowconf",),
    ),
    D(
        name="RAG_LOWCONF_MIN_SOURCES",
        domain="retrieval",
        value_type="int",
        default=1,
        minimum=0,
        maximum=100,
        description=(
            "Nombre de sources en dessous duquel la réponse est réputée de faible "
            "confiance, indépendamment des scores."
        ),
        consumers=("apps.rag_core.services.query.lowconf",),
    ),
    # --- Budget de prompt ----------------------------------------------------
    D(
        name="RAG_MAX_PROMPT_CHARS",
        domain="retrieval",
        value_type="int",
        default=18000,
        minimum=1000,
        maximum=1000000,
        description=(
            "Budget de caractères du prompt : c'est lui qui TRONQUE réellement le "
            "contexte transmis au modèle."
        ),
        consumers=("apps.rag_core.services.query.prompt_builder",),
        notes=(
            "Couplé à RAG_FACTUAL_NUM_CTX : le check `rag_core.W003` signale un prompt "
            "qui ne tient pas dans la fenêtre du modèle. "
            "Le budget porte sur le prompt ENTIER : consignes, sources et question "
            "puisent dans la même enveloppe. Une consigne ajoutée se paie donc en "
            "sources — mesuré : un bloc de format élargi de 573 caractères retirait une "
            "source à 92 % des requêtes. Arithmétique de référence (extraits de 800 c) : "
            "gabarit 1106 c + 8 sources × 823 c = 7690 c ; d'où la valeur qualifiée de "
            "**8300** en staging, qui laisse 610 c de marge — davantage que les 574 c "
            "d'avant le chantier d'intention. Le coût fixe du gabarit est borné par "
            "`test_prompt_minimum_source.py` (≤ 18 % du budget)."
        ),
    ),
    D(
        name="RAG_MIN_PROMPT_SOURCES",
        domain="retrieval",
        value_type="int",
        default=4,
        minimum=0,
        maximum=64,
        description=(
            "Nombre de sources dont la place est RÉSERVÉE dans le budget de prompt "
            "avant d'allouer ce qui reste à la mémoire de conversation."
        ),
        consumers=("apps.rag_core.services.query.prompt_builder",),
        notes=(
            "La mémoire était injectée dans la question AVANT l'allocation : elle "
            "entrait donc dans le coût réputé FIXE, et les sources étaient la seule "
            "variable d'ajustement. Mesuré le 23/08/2026 sur 25 fils réels : mémoire "
            "médiane de 3 045 caractères en staging, soit 3 sources perdues sur 8, et "
            "un fil descendu à ZÉRO source. La mémoire peut atteindre ~7 592 c "
            "(CHAT_MEMORY_SUMMARY_MAX_CHARS 4 000 + CHAT_MEMORY_MAX_CHARS 3 500, qui "
            "s'additionnent sans borne commune) contre un budget de 8 300. "
            "Le réglage inverse la priorité : les sources sont la substance d'un "
            "assistant documentaire, la mémoire une aide à la résolution de "
            "références. On peut répondre sans se souvenir ; on ne peut pas prouver "
            "sans source. Mettre 0 restaure le comportement antérieur."
        ),
    ),
    D(
        name="RAG_SNIPPET_MAX_CHARS",
        domain="retrieval",
        value_type="int",
        default=1200,
        minimum=100,
        maximum=100000,
        description="Taille maximale d'un extrait textuel inséré dans le prompt.",
        consumers=("apps.rag_core.services.query.prompt_builder",),
    ),
    D(
        name="RAG_SNIPPET_MAX_CHARS_TABLE",
        domain="retrieval",
        value_type="int",
        default=4000,
        minimum=100,
        maximum=100000,
        description="Taille maximale d'un extrait TABULAIRE inséré dans le prompt.",
        consumers=("apps.rag_core.services.query.prompt_builder",),
    ),
    D(
        name="RAG_SNIPPET_MAX_CHARS_STRUCTURED",
        domain="retrieval",
        value_type="int",
        default=4000,
        minimum=400,
        maximum=20000,
        description=(
            "Taille maximale de l'extrait d'un fragment d'extraction STRUCTURÉE "
            "(`meta.visual.structured` : graphique, tableau) inséré dans le prompt. Tient "
            "entier s'il rentre ; sinon lignes cohérentes (contexte, en-têtes de bloc, "
            "lignes appariées à la question) et couverture partielle DÉCLARÉE."
        ),
        consumers=("apps.rag_core.services.query.prompt_builder",),
        notes=(
            "Puise dans le budget GLOBAL "
            "du prompt comme toute source (retrait des moins prioritaires, rognage de la "
            "dernière). Ne touche ni `RAG_SNIPPET_MAX_CHARS` ni les autres extraits."
        ),
    ),
    D(
        name="RAG_SNIPPET_STRATEGY",
        domain="retrieval",
        value_type="enum",
        choices=("head", "centered"),
        default="head",
        description=(
            "FENÊTRE de l'extrait textuel inséré dans le prompt, à taille inchangée : "
            "`head` (N premiers caractères, historique) ou `centered` (mêmes N "
            "caractères autour de la phrase la plus proche de la question)."
        ),
        consumers=("apps.rag_core.services.query.prompt_builder",),
        notes=(
            "Ne change NI `RAG_SNIPPET_MAX_CHARS` NI le nombre de sources : seul le "
            "placement de la fenêtre change. Le défaut `head` reproduit le prompt "
            "historique à l'octet près. Mesuré au lot SNIPPET-TRUNCATION-1A : 77,7 % "
            "des chunks de production dépassent 800 caractères et 41 % des caractères "
            "retenus par le retrieval étaient supprimés en tête de chunk ; sur cinq "
            "échecs reproduits, `head` corrige 0/5 et `centered` 5/5 sans perdre une "
            "source. N'exige aucune migration ni réindexation — les extraits sont "
            "construits à la requête, donc les documents déjà indexés en bénéficient."
        ),
    ),
    D(
        name="RAG_PROMPT_RENDER_CONFORMANCE_V1_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=False,
        description=(
            "Conformité du RENDU du prompt : marqueur de troncature tabulaire compté "
            "dans le budget (C1), date/page non répétées dans l'en-tête de source (C2), "
            "compteurs de coût des règles conditionnelles (V4). OFF = rendu historique "
            "à l'octet près."
        ),
        consumers=("apps.rag_core.services.query.prompt_builder",),
        notes=(
            "Ne touche NI le retrieval, NI les modèles, NI les règles CLASSEURS/TABLEAUX, "
            "NI la politique du plus long préfixe, NI les budgets configurés. Sous OFF, "
            "les 105 prompts DEV de référence sont reproduits à l'empreinte SHA-256 près "
            "(lot PROMPT-RENDER-CONFORMANCE-FIX-1A). Sous ON, un extrait tabulaire "
            "tronqué ne dépasse plus le plafond en silence (mesuré : 8 350 > 8 300), un "
            "en-tête n'écrit plus « (date) p.N » deux fois, et `prompt_budget` expose "
            "`rules_kind`, `conditional_rules_chars`, `prompt_total_chars`, "
            "`prompt_remaining_chars`, `prompt_limit_exceeded`, "
            "`prompt_limit_exception_reason`. Rollback = remettre à 0 : aucune "
            "migration, aucune réindexation."
        ),
    ),
    D(
        name="RAG_EMAIL_UNTRUSTED_FRAMING_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=False,
        description=(
            "Cadrage « contenu non fiable » des courriels dans le prompt : chaque source "
            "courriel (corps ou pièce jointe) est marquée COURRIEL et un bloc de règles "
            "rappelle que son texte est une donnée, jamais une consigne. OFF = rendu "
            "historique à l'octet près. Sans source courriel dans le contexte, le prompt "
            "est identique à l'octet près même à ON."
        ),
        consumers=("apps.rag_core.domain.retrieval",),
        notes=(
            "S'applique aux courriels importés (.eml et pièces jointes). Atténue l'injection "
            "de prompt, ne la rend pas impossible."
        ),
    ),
    # --- Reranking -----------------------------------------------------------
    D(
        name="RAG_RERANK_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=False,
        description="Reranking cross-encoder des candidats avant troncature finale.",
        consumers=("apps.rag_core.services.rerank", "apps.rag_core.domain.policies"),
    ),
    D(
        name="RAG_RERANK_PROVIDER",
        domain="retrieval",
        value_type="enum",
        choices=("stub", "cross_encoder"),
        default="stub",
        description=(
            "Implémentation de reranking. Une valeur inconnue est refusée quand le "
            "reranking est actif : le stub tournerait sinon à la place du cross-encoder."
        ),
        consumers=("apps.rag_core.checks_rerank", "apps.rag_core.services.rerank"),
    ),
    D(
        name="RAG_RERANK_MODEL_NAME",
        domain="retrieval",
        value_type="str",
        default="BAAI/bge-reranker-base",
        description="Modèle cross-encoder de reranking (chargé localement).",
        consumers=("apps.rag_core.services.rerank",),
    ),
    D(
        name="RAG_RERANK_DEVICE",
        domain="retrieval",
        value_type="str",
        default="cuda",
        description="Périphérique d'inférence du reranker (`cuda` retombe sur CPU sans GPU).",
        consumers=("apps.rag_core.services.rerank",),
    ),
    D(
        name="RAG_RERANK_BATCH_SIZE",
        domain="retrieval",
        value_type="int",
        default=16,
        minimum=1,
        maximum=512,
        description="Taille de lot d'inférence du reranker.",
        consumers=("apps.rag_core.services.rerank",),
    ),
    D(
        name="RAG_RERANK_TOP_K",
        domain="retrieval",
        value_type="int",
        default=4,
        minimum=1,
        maximum=100,
        description="Nombre de passages conservés après reranking.",
        consumers=("apps.rag_core.services.rerank", "apps.rag_core.domain.policies"),
        notes="DOUBLON connu : défaut 5 dans `domain/policies.py`.",
    ),
    D(
        name="RAG_RERANK_TRIGGER_MIN_CANDIDATES",
        domain="retrieval",
        value_type="int",
        default=6,
        minimum=0,
        maximum=200,
        description="Nombre minimal de candidats en dessous duquel le reranking est inutile.",
        consumers=("apps.rag_core.services.rerank",),
    ),
    D(
        name="RAG_RERANK_MAX_TEXT_CHARS",
        domain="retrieval",
        value_type="int",
        default=2000,
        minimum=100,
        maximum=100000,
        description="Troncature du texte soumis au reranker (coût d'inférence).",
        consumers=("apps.rag_core.services.rerank",),
    ),
    # --- Reranking de passages via l'AI Gateway ------------------------------
    # Chemin DISTINCT des `RAG_RERANK_*` ci-dessus : celui-ci n'embarque aucun
    # modèle local, il ne connaît que le contrat Gateway et un nom LOGIQUE de
    # modèle. Ces sept réglages portent des décisions d'exploitation gouvernées
    # (activation, profondeur, plafond, délai, politique d'échec) : ils doivent
    # donc être déclarés ici, et pas seulement projetés dans les settings.
    D(
        name="RAG_RERANKER_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=False,
        description=(
            "Reranking de passages via l'AI Gateway (`/v1/rerank`). Distinct de "
            "`RAG_RERANK_ENABLED` : aucun modèle n'est chargé localement. OFF partout "
            "par défaut ; l'absence de la variable laisse le reranking éteint."
        ),
        consumers=(
            "apps.rag_core.services.rerank_gateway",
            "apps.rag_core.services.query.orchestrator",
        ),
    ),
    D(
        name="RAG_RERANKER_BASE_URL",
        domain="retrieval",
        value_type="url",
        default="",
        secret=True,
        redact_mode="url",
        description=(
            "URL de l'AI Gateway servant le reranking — jamais celle d'un backend de "
            "reranking. Vide et drapeau actif : l'appel échoue et la politique d'échec "
            "s'applique."
        ),
        consumers=("apps.rag_core.services.rerank_gateway",),
    ),
    D(
        name="RAG_RERANKER_MODEL_LOGICAL",
        domain="retrieval",
        value_type="str",
        default="rerank-default",
        description=(
            "Nom LOGIQUE du modèle de reranking. RagATM n'utilise jamais le nom du "
            "dépôt Hugging Face : la correspondance appartient à la Gateway."
        ),
        consumers=("apps.rag_core.services.rerank_gateway",),
    ),
    D(
        name="RAG_RERANKER_POOL_K",
        domain="retrieval",
        value_type="int",
        default=40,
        minimum=1,
        maximum=64,
        description=(
            "Profondeur de candidats soumise au reranking, prélevée sur l'ordre RRF "
            "CANONIQUE. Le maximum reflète la limite de documents du contrat Gateway."
        ),
        consumers=("apps.rag_core.services.rerank_gateway",),
    ),
    D(
        name="RAG_RERANKER_TIMEOUT_SECONDS",
        domain="retrieval",
        value_type="float",
        default=2.0,
        minimum=0.1,
        maximum=60.0,
        description=(
            "Délai DÉDIÉ de l'appel de reranking, jamais partagé avec le LLM. Dépassé, "
            "l'échec est compté séparément (`rag_reranker_timeout_total`)."
        ),
        consumers=("apps.rag_core.services.rerank_gateway",),
    ),
    D(
        name="RAG_RERANKER_FAILURE_POLICY",
        domain="retrieval",
        value_type="enum",
        choices=("fail_open_rrf", "fail_closed"),
        default="fail_open_rrf",
        description=(
            "Conduite en cas d'échec du reranking. `fail_open_rrf` : l'ordre RRF est "
            "conservé, la requête aboutit, le repli est COMPTÉ. `fail_closed` : l'erreur "
            "remonte — réservé aux qualifications, où un repli mélangerait baseline et "
            "reranking dans les mêmes chiffres."
        ),
        consumers=("apps.rag_core.services.rerank_gateway",),
    ),
    D(
        name="RAG_RERANKER_MAX_PER_GROUP",
        domain="retrieval",
        value_type="int",
        default=3,
        minimum=1,
        maximum=50,
        description=(
            "Plafond par document du chemin reranké UNIQUEMENT. Drapeau éteint, repli ou "
            "échec : le plafond historique `RAG_RETRIEVAL_MAX_PER_GROUP` reste seul en "
            "vigueur. Relâcher le plafond n'a de sens que si ce qu'il laisse entrer est "
            "bien classé."
        ),
        consumers=(
            "apps.rag_core.services.rerank_gateway",
            "apps.rag_core.services.query.orchestrator",
        ),
    ),
    # --- Policy d'admission en mode fantôme -----------------------------------
    D(
        name="RAG_ADMISSION_SHADOW_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=False,
        description=(
            "Calcule la policy d'admission `balanced-v1` en OBSERVATION et l'inscrit "
            "dans la trace de retrieval. N'influence RIEN : ni la réponse, ni le "
            "prompt, ni le classement, ni les sources, ni le statut HTTP — avec ce "
            "drapeau ON ou OFF, la réponse servie est identique, et un test le "
            "verrouille. Aucun appel modèle supplémentaire : la policy relit le score "
            "déjà produit par le reranking. Les SEUILS ne sont pas configurables — les "
            "déplacer exige une nouvelle `POLICY_VERSION` (cf. "
            "`apps.rag_core.domain.admission_shadow`)."
        ),
        consumers=(
            "apps.rag_core.domain.admission_shadow",
            "apps.rag_core.services.query.orchestrator",
        ),
    ),
    # --- Trace des candidats (diagnostic) --------------------------------------
    D(
        name="RAG_CANDIDATE_TRACE_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=False,
        description=(
            "Inscrit dans la trace de retrieval le DÉTAIL des candidats du pool : "
            "identifiant, document, magasin d'origine (privé ou corpus métier), "
            "méthodes de retrieval, scores lexical/vectoriel/fusion/reranker séparés, "
            "et rangs avant et après reranking. Capacité d'OBSERVATION : rien ne la "
            "lit — ni l'admission, ni le classement, ni la réponse — et un test "
            "compare les résultats drapeau ON et OFF. Aucun appel modèle "
            "supplémentaire : la trace relit des champs déjà posés par le pipeline. "
            "Aucun texte métier n'y figure (identifiants, rangs et scores seulement). "
            "Existe parce qu'une version antérieure n'avait persisté qu'un scalaire par requête, ce "
            "qui a rendu indécidable la part d'un défaut de rappel et d'un défaut "
            "d'ordonnancement (cf. `services.query.candidate_trace`)."
        ),
        consumers=(
            "apps.rag_core.services.query.candidate_trace",
            "apps.rag_core.services.query.orchestrator",
        ),
    ),
    # --- Answerability -------------------------------------------------------
    D(
        name="RAG_ANSWERABILITY_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=False,
        description=(
            "Décide s'il faut RÉPONDRE (distinct du reranking, qui ORDONNE). Statut : "
            "mesuré, NON promu — aucun seuil ne passe le gate sur les deux corpus."
        ),
        consumers=("apps.rag_core.services.answerability_ce",),
    ),
    D(
        name="RAG_ANSWERABILITY_PROVIDER",
        domain="retrieval",
        value_type="str",
        default="cross_encoder",
        description="Implémentation d'answerability (local uniquement).",
        consumers=("apps.rag_core.services.answerability_ce",),
    ),
    D(
        name="RAG_ANSWERABILITY_MODEL",
        domain="retrieval",
        value_type="str",
        default="BAAI/bge-reranker-base",
        description="Modèle cross-encoder d'answerability. Aucun repli vers une API distante.",
        consumers=("apps.rag_core.services.answerability_ce",),
    ),
    D(
        name="RAG_ANSWERABILITY_DEVICE",
        domain="retrieval",
        value_type="str",
        default="cpu",
        description="Périphérique d'inférence de l'answerability (~300 ms/paire en CPU).",
        consumers=("apps.rag_core.services.answerability_ce",),
    ),
    D(
        name="RAG_ANSWERABILITY_THRESHOLD",
        domain="retrieval",
        value_type="float",
        default=0.5,
        minimum=0.0,
        maximum=1.0,
        description=(
            "Seuil de décision sur un score SIGMOÏDE dans [0, 1] — ce n'est pas un logit."
        ),
        consumers=("apps.rag_core.services.answerability_ce",),
    ),
    D(
        name="RAG_ANSWERABILITY_STRATEGY",
        domain="retrieval",
        value_type="enum",
        choices=("max", "mean_top2", "max_guarded", "composite"),
        default="max",
        description="Agrégation des scores de paires en une décision unique.",
        consumers=("apps.rag_core.services.answerability_ce",),
    ),
    D(
        name="RAG_ANSWERABILITY_TOP_K",
        domain="retrieval",
        value_type="int",
        default=5,
        minimum=1,
        maximum=50,
        description="Nombre de passages soumis à l'answerability (top-10 dépasse le budget).",
        consumers=("apps.rag_core.services.answerability_ce",),
    ),
    D(
        name="RAG_ANSWERABILITY_MAX_TEXT_CHARS",
        domain="retrieval",
        value_type="int",
        default=2000,
        minimum=100,
        maximum=100000,
        description="Troncature du texte soumis à l'answerability.",
        consumers=("apps.rag_core.services.answerability_ce",),
    ),
    D(
        name="RAG_ANSWERABILITY_TIMEOUT_SECONDS",
        domain="retrieval",
        value_type="float",
        default=3.0,
        minimum=0.1,
        maximum=120.0,
        description="Budget d'inférence de l'answerability.",
        consumers=("apps.rag_core.services.answerability_ce",),
    ),
    D(
        name="RAG_ANSWERABILITY_FAIL_OPEN",
        domain="retrieval",
        value_type="bool",
        default=True,
        description=(
            "Une panne du modèle ne se transforme pas en refus massif ; "
            "l'indisponibilité est TRACÉE (`decision=unavailable`)."
        ),
        consumers=("apps.rag_core.services.answerability_ce",),
    ),
    # --- Réécriture de requête (lue hors settings) ---------------------------
    D(
        name="RAG_QUERY_REWRITE_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=False,
        description=(
            "Réécriture LLM de la question avant recherche, en mode explicatif "
            "uniquement. Ajoute une requête reformulée et une requête keywordisée au "
            "plan de recherche — donc deux passes de retrieval supplémentaires."
        ),
        source="backend/apps/rag_core/services/query/rewrite.py",
        consumers=("apps.rag_core.services.query.rewrite",),
        notes=(
            "Lue directement dans l'environnement à l'import du module (non déclarée en "
            "settings) : un changement exige un redémarrage, pas un override. "
            "**Défaut passé à 0 le 30/07/2026** après mesure A/B sur 100 questions, même "
            "code et même base : aucune source indispensable récupérée ni perdue, "
            "décisions d'admission IDENTIQUES (VP 29, FP 1, VN 34, FN 6), 570 sources "
            "dans les deux bras, 53 documents apportés contre 49 retirés — une "
            "permutation. Coût mesuré : P95 +102,6 %. Le modèle traduisait la question "
            "en français sur un fonds anglophone. Le coût vient des deux passes "
            "supplémentaires (FTS de la requête keywordisée : 2,7 s), pas de l'appel LLM "
            "(0,3 s à chaud) : réduire son timeout n'y changerait rien. Réactiver impose "
            "de re-mesurer, le verdict dépendant de la langue du fonds."
        ),
    ),
    D(
        name="RAG_QUERY_REWRITE_PROVIDER",
        domain="retrieval",
        value_type="str",
        default="ollama",
        description="Provider de la réécriture de requête.",
        source="backend/apps/rag_core/services/query/rewrite.py",
        consumers=("apps.rag_core.services.query.rewrite",),
    ),
    D(
        name="RAG_QUERY_REWRITE_MODEL",
        domain="retrieval",
        value_type="str",
        default="",
        description="Modèle de réécriture. Vide => modèle par défaut du provider.",
        source="backend/apps/rag_core/services/query/rewrite.py",
        consumers=("apps.rag_core.services.query.rewrite",),
    ),
    D(
        name="RAG_QUERY_REWRITE_TIMEOUT_S",
        domain="retrieval",
        value_type="float",
        default=6.0,
        minimum=0.1,
        maximum=120.0,
        description="Budget d'appel de la réécriture de requête.",
        source="backend/apps/rag_core/services/query/rewrite.py",
        consumers=("apps.rag_core.services.query.rewrite",),
    ),
    D(
        name="RAG_QUERY_REWRITE_MAX_CHARS",
        domain="retrieval",
        value_type="int",
        default=140,
        minimum=10,
        maximum=10000,
        description="Longueur maximale d'une requête réécrite.",
        source="backend/apps/rag_core/services/query/rewrite.py",
        consumers=("apps.rag_core.services.query.rewrite",),
    ),
    D(
        name="RAG_MAX_SEARCH_QUERIES",
        domain="retrieval",
        value_type="int",
        default=3,
        minimum=1,
        maximum=20,
        description="Nombre maximal de requêtes de recherche dérivées d'une question.",
        source="backend/apps/rag_core/services/query/rewrite.py",
        consumers=("apps.rag_core.services.query.rewrite",),
    ),
    D(
        name="RAG_KEYWORDIZE_ENABLED",
        domain="retrieval",
        value_type="bool",
        default=True,
        description="Extraction de mots-clés en complément de la réécriture.",
        source="backend/apps/rag_core/services/query/rewrite.py",
        consumers=("apps.rag_core.services.query.rewrite",),
    ),
    D(
        name="RAG_KEYWORDIZE_MAX_TOKENS",
        domain="retrieval",
        value_type="int",
        default=10,
        minimum=1,
        maximum=100,
        description="Nombre maximal de mots-clés extraits.",
        source="backend/apps/rag_core/services/query/rewrite.py",
        consumers=("apps.rag_core.services.query.rewrite",),
    ),
)
