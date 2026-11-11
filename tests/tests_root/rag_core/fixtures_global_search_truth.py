"""Corpus de vérité terrain pour la recherche globale — ANNOTÉ À LA MAIN.

Aucun LLM n'a servi à produire cette annotation : c'est le point de la vérité
terrain. Elle sert à mesurer la précision et le rappel du PRÉFILTRE LEXICAL,
indépendamment de la qualité du LLM.

Générique et sans domaine : aucun vocabulaire de finance, de construction, de
droit ni d'assurance. Les sujets sont des notions ordinaires d'organisation
(délais, sécurité, formation, matériel, transport…).

Couvre : correspondances exactes, synonymes, variantes morphologiques,
paraphrases, accents et absence d'accents, termes rares, termes fréquents,
documents hors sujet partageant un terme, documents sans résultat, doublons,
document FAILED, document READY sans extrait, document d'un autre RagSpace,
français, anglais, texte mixte, fautes de frappe, négations, occurrences
multiples.
"""

# --------------------------------------------------------------------------- #
# Documents. `key` = identifiant stable utilisé par la vérité terrain.
# `status` : READY (défaut) | FAILED. `chunks` : [] = READY sans extrait.
# `space` : "main" (défaut) | "other" (autre RagSpace — ne doit JAMAIS remonter).
# --------------------------------------------------------------------------- #
DOCUMENTS = [
    # --- Sujet « délai » : exact, morphologie, synonyme, paraphrase ----------
    {"key": "d01", "chunks": [
        "Les délais de livraison ont été respectés sur l'ensemble du trimestre.",
    ]},
    {"key": "d02", "chunks": [
        "Un délai supplémentaire a été accordé au prestataire.",
        "Ce délai court jusqu'à la fin du mois.",   # occurrences multiples
    ]},
    {"key": "d03", "chunks": [
        "Le retard constaté sur la livraison provient du transporteur.",  # synonyme
    ]},
    {"key": "d04", "chunks": [
        "La livraison a été effectuée sans delai particulier.",  # SANS accent
    ]},

    # --- Sujet « sécurité » : accents, variantes, anglais --------------------
    {"key": "d05", "chunks": [
        "La sécurité des accès au bâtiment a été renforcée.",
    ]},
    {"key": "d06", "chunks": [
        "Les consignes de sécurité sont affichées à chaque étage.",
    ]},
    {"key": "d07", "chunks": [
        "Security procedures were reviewed by the internal team.",  # anglais
    ]},
    {"key": "d08", "chunks": [
        "Le dispositif de securite incendie est contrôlé chaque année.",  # sans accent
    ]},

    # --- Sujet « formation » : fréquent, avec un hors-sujet partageant un mot -
    {"key": "d09", "chunks": [
        "Un plan de formation a été proposé à l'ensemble des équipes.",
    ]},
    {"key": "d10", "chunks": [
        "La formation continue reste une priorité de l'organisation.",
    ]},
    {"key": "d11", "chunks": [
        "La formation du nuage se produit lorsque l'air se refroidit.",
        # « formation » au sens géologique : HORS SUJET, partage pourtant le terme.
    ]},

    # --- Sujet rare : « hélicoptère » (un seul document) ---------------------
    {"key": "d12", "chunks": [
        "Un hélicoptère a été affrété pour l'intervention en zone isolée.",
    ]},

    # --- Sujet « transport » et « véhicule » : proches mais distincts --------
    {"key": "d13", "chunks": [
        "Le transport des équipements est assuré par un prestataire externe.",
    ]},
    {"key": "d14", "chunks": [
        "Les véhicules de service font l'objet d'un entretien trimestriel.",
    ]},

    # --- Négation : mentionne le terme, mais le nie --------------------------
    {"key": "d15", "chunks": [
        "Aucun incident de sécurité n'a été signalé durant la période.",
    ]},

    # --- Faute de frappe raisonnable ----------------------------------------
    {"key": "d16", "chunks": [
        "Les délias de traitement ont été revus à la baisse.",  # « délias » (typo)
    ]},

    # --- Doublon exact de d05 (autre document, même texte) -------------------
    {"key": "d17", "chunks": [
        "La sécurité des accès au bâtiment a été renforcée.",
    ]},

    # --- Documents franchement hors sujet ------------------------------------
    {"key": "d18", "chunks": ["Le menu de la cantine change chaque semaine."]},
    {"key": "d19", "chunks": ["La bibliothèque ouvre du lundi au vendredi."]},
    {"key": "d20", "chunks": ["Les plantes du hall sont arrosées le mardi."]},
    {"key": "d21", "chunks": ["Le tournoi interne aura lieu au printemps."]},

    # --- Multilingue dans un même document -----------------------------------
    {"key": "d22", "chunks": [
        "Le budget est validé. The delivery schedule was confirmed by the supplier.",
    ]},

    # --- Termes fréquents (bruit) : présents partout, ne discriminent rien ----
    {"key": "d23", "chunks": [
        "Le projet avance selon le plan établi par l'équipe.",
    ]},
    {"key": "d24", "chunks": [
        "L'équipe projet se réunit chaque semaine pour faire le point.",
    ]},

    # --- Cas techniques : exclus du périmètre exploitable ---------------------
    # READY mais SANS extrait -> considéré, jamais analysé.
    {"key": "d25", "chunks": []},
    # FAILED -> considéré, jamais analysé, couverture incomplète.
    {"key": "d26", "status": "FAILED", "chunks": []},
    # Autre RagSpace -> ne doit JAMAIS remonter, même en parlant du sujet.
    {"key": "d27", "space": "other", "chunks": [
        "La sécurité du site voisin a été auditée. Les délais sont tenus.",
    ]},
]


# --------------------------------------------------------------------------- #
# Vérité terrain. `expected_relevant` : documents qu'un préfiltre honnête DOIT
# ramener. `expected_irrelevant` : documents qu'il ne doit PAS ramener (pièges).
#
# Note importante : le rappel est mesuré sur ce que le préfiltre LEXICAL peut
# atteindre. Un synonyme (« retard » pour « délai ») ou une faute de frappe
# (« délias ») ne sont PAS lexicalement atteignables — ils sont listés dans
# `lexically_unreachable` pour que la limite soit MESURÉE, pas masquée.
# C'est exactement la frontière entre couverture B (garantie) et C (probabiliste).
# --------------------------------------------------------------------------- #
BENCHMARK_QUERIES = [
    # ---- 3 requêtes RARES (fort pouvoir discriminant) ----
    {
        "id": "rare-1",
        "query": "recherche dans toute la base les informations sur l'hélicoptère",
        "expected_relevant": ["d12"],
        "expected_irrelevant": ["d13", "d14", "d18"],
        "lexically_unreachable": [],
        "kind": "rare",
    },
    {
        "id": "rare-2",
        "query": "analyse dans tous les documents ce qui concerne la cantine",
        "expected_relevant": ["d18"],
        "expected_irrelevant": ["d19", "d20"],
        "lexically_unreachable": [],
        "kind": "rare",
    },
    {
        "id": "rare-3",
        "query": "que dit l'ensemble du corpus sur la bibliothèque",
        "expected_relevant": ["d19"],
        "expected_irrelevant": ["d18", "d21"],
        "lexically_unreachable": [],
        "kind": "rare",
    },

    # ---- 3 requêtes MOYENNES ----
    {
        "id": "medium-1",
        "query": "recherche dans toute la base les informations sur les délais de livraison",
        "expected_relevant": ["d01", "d02", "d03", "d04"],
        "expected_irrelevant": ["d18", "d19", "d20", "d21"],
        # « retard » (d03) est un synonyme : hors de portée d'un préfiltre lexical.
        # « délias » (d16) est une typo : idem.
        "lexically_unreachable": ["d16"],
        "kind": "medium",
    },
    {
        "id": "medium-2",
        "query": "analyse tout le corpus concernant le transport des équipements",
        "expected_relevant": ["d13"],
        # d23/d24 parlent d'« équipe » : le stemmer français réduit « équipements »
        # ET « équipe » au même radical « équip ». Un préfiltre LEXICAL les ramènera
        # donc nécessairement. Collision RÉELLE, comptée comme un faux positif : la
        # précision mesurée en tient compte, elle n'est pas embellie.
        "expected_irrelevant": ["d18", "d19"],
        "stem_collisions": ["d23", "d24"],
        "lexically_unreachable": [],
        "kind": "medium",
    },
    {
        "id": "medium-3",
        "query": "recense dans tous les documents les informations sur les véhicules",
        "expected_relevant": ["d14"],
        "expected_irrelevant": ["d13", "d18"],
        "lexically_unreachable": [],
        "kind": "medium",
    },

    # ---- 3 requêtes LARGES (termes fréquents — c'est là que le OU explose) ----
    {
        "id": "broad-1",
        "query": "recherche dans toute la base les informations sur la sécurité",
        "expected_relevant": ["d05", "d06", "d15", "d17"],
        "expected_irrelevant": ["d18", "d19", "d20", "d21"],
        # d07 « security » : anglais. d08 « securite » : sans accent, et NON atteint
        # ici — 'sécurité'->'sécur' ne rejoint pas 'securite'->'securit'. Le
        # repliement d'accent du stemmer français est INCOHÉRENT (il fonctionne pour
        # « délais »/« delai », pas pour « sécurité »/« securite »). On ne peut donc
        # jamais garantir qu'une variante accentuée sera trouvée.
        "lexically_unreachable": ["d07", "d08"],
        "kind": "broad",
    },
    {
        "id": "broad-2",
        "query": "analyse dans tous les documents ce qui concerne la formation",
        # d11 partage le terme mais parle de météo : un préfiltre LEXICAL le
        # ramènera nécessairement. C'est une limite mesurée, pas un bug — le
        # traitement par lots doit l'écarter.
        "expected_relevant": ["d09", "d10"],
        "expected_irrelevant": ["d18", "d19"],
        "lexically_unreachable": [],
        "kind": "broad",
    },
    {
        "id": "broad-3",
        "query": "que dit tout le corpus sur le projet et l'équipe",
        # d09 (« ensemble des équipes ») et d13 (« équipements ») partagent le radical
        # « équip » : ils sont lexicalement atteints, et d09 parle bien d'équipes.
        "expected_relevant": ["d23", "d24"],
        "expected_irrelevant": ["d18", "d19", "d20"],
        "stem_collisions": ["d09", "d13"],
        "lexically_unreachable": [],
        "kind": "broad",
    },

    # ---- 3 requêtes PARAPHRASÉES (le sujet n'est pas dit avec les mots du doc) ----
    {
        "id": "para-1",
        "query": "recherche dans tous les documents les informations sur les retards",
        "expected_relevant": ["d03"],
        "expected_irrelevant": ["d18", "d19"],
        # d01/d02 parlent de « délais » : un préfiltre lexical ne fera pas le lien.
        "lexically_unreachable": ["d01", "d02"],
        "kind": "paraphrase",
    },
    {
        "id": "para-2",
        "query": "analyse toute la base sur l'entretien des voitures de service",
        "expected_relevant": [],
        "expected_irrelevant": ["d18", "d19"],
        # d14 dit « véhicules », pas « voitures ».
        "lexically_unreachable": ["d14"],
        "kind": "paraphrase",
    },
    {
        "id": "para-3",
        "query": "recherche dans tout le corpus les informations sur l'apprentissage",
        "expected_relevant": [],
        "expected_irrelevant": ["d18", "d19"],
        # d09/d10 disent « formation ».
        "lexically_unreachable": ["d09", "d10"],
        "kind": "paraphrase",
    },

    # ---- 2 requêtes ANGLAISES ----
    {
        "id": "en-1",
        "query": "search the whole corpus for information about security",
        "expected_relevant": ["d07"],
        "expected_irrelevant": ["d18", "d19"],
        # d05/d06 disent « sécurité » : hors de portée lexicale depuis l'anglais.
        "lexically_unreachable": ["d05", "d06", "d08"],
        "kind": "english",
    },
    {
        "id": "en-2",
        "query": "analyze the entire corpus for information about delivery",
        "expected_relevant": ["d22"],
        "expected_irrelevant": ["d18", "d19"],
        "lexically_unreachable": ["d01", "d03"],
        "kind": "english",
    },

    # ---- 2 requêtes AVEC ACCENTS (la variante sans accent est-elle atteinte ?) ----
    {
        "id": "accent-1",
        "query": "recherche dans toute la base les informations sur la sécurité incendie",
        "expected_relevant": ["d08"],
        "expected_irrelevant": ["d18", "d19"],
        "lexically_unreachable": [],
        "kind": "accent",
    },
    {
        "id": "accent-2",
        "query": "analyse tout le corpus sur les délais",
        # d04 écrit « delai » SANS accent, et il EST pourtant retrouvé : le stemmer
        # français réduit « délais » ET « delai » au même radical « del ».
        # Mais ce repliement est INCOHÉRENT — « sécurité » -> « sécur » ne rejoint
        # PAS « securite » -> « securit » (cf. broad-1/d08). On ne peut donc pas
        # compter sur les variantes accentuées : c'est constaté mot par mot.
        "expected_relevant": ["d01", "d02", "d04"],
        "expected_irrelevant": ["d18", "d19"],
        "lexically_unreachable": [],
        "kind": "accent",
    },
]

assert len(BENCHMARK_QUERIES) >= 16, "au moins 16 requêtes de benchmark exigées"
assert len(DOCUMENTS) >= 20, "au moins 20 documents exigés"
