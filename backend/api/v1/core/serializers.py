from rest_framework import serializers

from api.v1.limits import MAX_QUESTION_CHARS


class CoreRetrieveSerializer(serializers.Serializer):
    """Entrée de POST /api/v1/core/retrieve/.

    Minimal et fidèle au moteur actuel : `rag_space_id` + `question`. Le scoping
    par (project, environment) et les filtres (status/confidentiality/top_k) du
    contrat cible sont volontairement DIFFÉRÉS (ils impliqueraient de toucher le
    retrieval, hors périmètre PR 3).
    """

    rag_space_id = serializers.UUIDField()
    #: Même borne que le chat : `/core/retrieve` délègue à `query_rag`, donc au
    #: même classifieur d'intention. Borner une seule des deux portes ne
    #: fermerait rien.
    question = serializers.CharField(max_length=MAX_QUESTION_CHARS)
