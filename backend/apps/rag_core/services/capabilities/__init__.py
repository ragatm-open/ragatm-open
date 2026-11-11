"""Capacités documentaires EFFECTIVES — observation, jamais pilotage.

- ``registry`` : ce que RagATM SAIT faire (déclaratif, relié aux extracteurs et aux flags) ;
- ``resolver`` : ce qui est ACTIF ICI ET MAINTENANT (accesseurs et résolveurs de portée existants) ;
- ``runtime`` : empreinte de configuration publiée par chaque worker (Redis, TTL) ;
- ``gateway_health`` : sondes ``GET /health/*`` d'AI Gateway (aucune inférence).

Aucun module de ce paquet ne modifie un flag, une liste d'espaces, un document ou un modèle.
"""
