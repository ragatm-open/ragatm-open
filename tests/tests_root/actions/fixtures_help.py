"""Vérité terrain de la détection d'aide — fixture PARTAGÉE (Lot 1).

Source unique du corpus : les tests unitaires du matcher, les tests de priorité du
routeur et les tests HTTP consomment tous ces listes. Aucun corpus recopié ailleurs.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# POSITIFS — doivent produire ``help.capabilities``.
# ---------------------------------------------------------------------------
HELP_POSITIVES: tuple[str, ...] = (
    # Niveau 1 — commandes exactes
    "/aide",
    "/help",
    "/?",
    "?",
    "aide",
    "help",
    "/exemples",
    # Niveau 2 — formulations naturelles ancrées
    "que puis-je faire ?",
    "qu'est-ce que je peux faire ?",
    "que pouvez-vous faire ?",
    "que puis-je demander ?",
    "quelles sont les fonctionnalités disponibles ?",
    "quelles fonctionnalités sont disponibles sous RagATM ?",
    "quelles commandes sont disponibles ?",
    "quelles sont les possibilités ?",
    "montre-moi des exemples de requêtes",
    "exemples de requêtes",
    "exemples de questions",
    "comment utiliser RagATM ?",
    "comment fonctionne RagATM ?",
    "what can I ask?",
    "what can I do?",
    "help me use RagATM",
    "how do I use RagATM?",
)

# ---------------------------------------------------------------------------
# NÉGATIFS — ne doivent JAMAIS produire ``help.capabilities``.
# « ? » isolé n'apparaît PAS ici : il est positif (commande exacte).
# ---------------------------------------------------------------------------
HELP_NEGATIVES: tuple[str, ...] = (
    "aide-moi à résumer ce document",
    "aidez-moi à comprendre ce contrat",
    "donne-moi de l'aide sur ce contrat",
    "help me summarize this document",
    "help me understand this agreement",
    "quelles fonctionnalités sont décrites dans ce PDF ?",
    "quelles commandes Linux sont citées dans le dossier ?",
    "quelles sont les fonctionnalités du produit décrit dans le rapport ?",
    "liste les documents sur Nordexia",
    "résume Rapport.pdf",
    "résume la page 3",
    "quoi de neuf sur Valteria ?",
    "donne-moi les news UniCredit",
    "quelle est la durée du contrat ?",
    "exemples de clauses dans ce contrat",
    "comment fonctionne la garantie décrite dans le PDF ?",
    "comment utiliser la fonction décrite dans ce document ?",
    "quelles possibilités offre ce contrat ?",
)

# ---------------------------------------------------------------------------
# Non-régression : certains négatifs doivent CONSERVER leur intention actuelle.
# Le but n'est pas seulement « pas help », mais « le comportement existant tient ».
# ---------------------------------------------------------------------------
HELP_NEGATIVES_EXPECTED_ACTION: tuple[tuple[str, str], ...] = (
    ("aide-moi à résumer ce document", "document.summarize"),
    ("résume Rapport.pdf", "document.summarize"),
    ("liste les documents sur Nordexia", "search.list_documents"),
    ("quelle est la durée du contrat ?", "search.answer"),
    ("aidez-moi à comprendre ce contrat", "search.answer"),
)
