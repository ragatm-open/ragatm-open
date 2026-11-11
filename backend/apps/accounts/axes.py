"""Identifiant surveillé par django-axes (repris d'un projet interne).

L'API se connecte par `email` : sans ce résolveur, Axes enregistrerait un
identifiant nul et ne compterait que par adresse IP. L'identifiant est normalisé
en minuscules — alterner les casses ne doit pas multiplier le quota d'essais.
Le Django admin se connecte par `username` : même résolveur, autre clé.
"""


def resolve_login_username(request, credentials=None) -> str | None:
    candidates = []

    if credentials:
        candidates.extend([credentials.get("email"), credentials.get("username")])

    data = getattr(request, "data", None)
    if not isinstance(data, dict):
        data = getattr(request, "POST", None)
    if hasattr(data, "get"):
        candidates.extend([data.get("email"), data.get("username")])

    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None
