"""Benchmark qualité RAG (Priorité B).

Sous-module *versionné* et *reproductible* pour mesurer objectivement la qualité
du retrieval (recall@k, MRR, précision des sources, no-answer accuracy, latences),
distinct des tests unitaires de contrat.

Points d'entrée :
- ``dataset.load_dataset`` : charge/valide un fichier JSONL de cas de test ;
- ``metrics`` : fonctions pures (recall@k, MRR, precision, percentiles) ;
- ``runner.run_benchmark`` : exécute les cas via le pipeline réel et agrège ;
- ``report`` : rend un rapport JSON + Markdown.

La commande ``manage.py rag_benchmark`` orchestre le tout.
"""
