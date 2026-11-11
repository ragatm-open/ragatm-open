"""Support de test partagé de la suite backend.

Ce paquet vit sous `backend/` parce que c'est un répertoire présent en tête du
`sys.path` de la suite : le conftest de `tests_root` y insère `backend/` explicitement.
Un fournisseur de fixtures logé ici est donc importable par tous les tests backend.

Historique : ces fixtures ont été introduites pour converger deux arbres de tests
(`tests_root` et l'ancien `tests_legacy`, désormais fusionné). Elles restent le point
unique de définition des fixtures partagées.
"""
