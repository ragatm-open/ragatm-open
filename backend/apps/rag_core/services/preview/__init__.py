# backend/apps/rag_core/services/preview/__init__.py
"""Aperçu tabulaire borné d'un classeur `.xlsx`.

Sert la VÉRIFICATION d'une citation : montrer les cellules d'où vient un chiffre,
avec quelques lignes de contexte, sans jamais charger le classeur entier ni
recalculer quoi que ce soit.
"""
