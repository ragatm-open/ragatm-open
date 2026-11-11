"""Capture diagnostique — ce qui manquait pour comprendre une mauvaise réponse.

LE PROBLÈME QUE CE PAQUET RÉSOUT
--------------------------------
Les candidats du retrieval n'existent QUE pendant l'exécution de ``query_rag``.
Ils vivent dans une variable locale, et disparaissent au ``return``. Une tâche
déclenchée au clic sur le pouce — trente secondes ou trois jours plus tard — ne
peut donc rien récupérer : il n'y a plus rien à récupérer.

Conséquence directe : il n'existe que deux façons honnêtes d'obtenir la trace.

1. **La capturer pendant qu'elle existe** (``capture`` + ``buffer``). C'est la
   seule capture EXACTE. Elle a un coût — on écrit pour toutes les réponses,
   alors que seule une infime minorité sera signalée — donc elle est bornée au
   strict minimum : identifiants, rangs, scores, raisons d'exclusion. **Aucun
   texte.** Et elle expire toute seule.
2. **La rejouer plus tard** (lot 2, tâche Celery). Ce n'est PAS une capture :
   l'index, la configuration et les modèles ont pu changer. Un replay est
   toujours étiqueté comme tel (``capture_mode="replay"``), jamais présenté
   comme l'état d'origine.

Ce paquet ne modifie NI les scores, NI les rangs, NI la sélection, NI la
politique de réponse. Il observe.
"""
