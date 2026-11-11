"""Corpus de détection L3 — partagé entre tests de matcher, de routeur et HTTP.

Aucun corpus n'est recopié ailleurs.
"""

# Demandes portant explicitement sur TOUT l'espace documentaire.
GLOBAL_POSITIVES = (
    "analyse tous les documents",
    "analyse l'ensemble des documents",
    "analyse toute la base documentaire",
    "analyse tout le corpus",
    "fais une synthèse de tous les documents",
    "fais une synthèse de l'ensemble du corpus",
    "résume toute la base documentaire",
    "donne-moi une vision globale de tous les documents",
    "recense toutes les informations disponibles",
    "liste tous les éléments présents dans la base",
    "parcours tous les documents",
    "recherche dans l'ensemble des documents",
    "ne te limite pas aux sources les plus pertinentes",
    "prends en compte toute la base",
    "je veux une analyse complète du corpus",
    "je veux une analyse de l'intégralité des documents",
    "analyse l'ensemble du RagSpace",
    "scan all documents",
    "analyze the entire corpus",
    "summarize all documents",
    "search across the whole corpus",
    "use all documents",
)

# La portée est restreinte (un document, une page, un fichier nommé) ou
# thématique (« … sur Nordexia ») : JAMAIS une demande globale.
# Le mot « tous » seul ne suffit jamais.
GLOBAL_NEGATIVES = (
    "analyse ce document",
    "analyse Rapport.pdf",
    "résume ce document",
    "résume la page 3",
    "analyse tous les risques de ce contrat",
    "liste tous les documents sur Nordexia",
    "quels sont tous les risques mentionnés dans ce document",
    "donne-moi toutes les informations sur UniCredit",
    "fais une analyse complète de cette page",
    "analyse l'ensemble des clauses de ce contrat",
    "recherche dans tous les paragraphes de ce fichier",
    "résume tous les chapitres de Rapport.pdf",
    "quels sont les principaux sujets du document",
    "analyse les documents les plus pertinents",
    "analyse plusieurs documents",
    "compare ces deux documents",
)

# Intentions existantes qui doivent être PRÉSERVÉES (non-régression prouvée).
PRESERVED_INTENTS = (
    ("résume tous les documents", "corpus.global_request"),
    ("résume Rapport.pdf", "document.summarize"),
    ("résume ce document", "document.summarize"),
    ("liste tous les documents sur Nordexia", "search.list_documents"),
    ("analyse ce document", "search.answer"),
    ("quelle est la durée du contrat ?", "search.answer"),
    ("/aide", "help.capabilities"),
)

QUICK_COMMANDS = (
    "réponse rapide",
    "mode rapide",
    "rapide",
    "continue en mode rapide",
    "quick answer",
    "Réponse rapide.",
)

FULL_COMMANDS = (
    "analyse complète",
    "mode complet",
    "analyse exhaustive",
    "full analysis",
)
