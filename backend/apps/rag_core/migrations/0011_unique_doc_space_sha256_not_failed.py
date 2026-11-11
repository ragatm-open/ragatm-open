from django.db import migrations


class Migration(migrations.Migration):
    # NOTE: This migration is intentionally a NO-OP.
    #
    # Historiquement elle (ré)ajoutait la contrainte
    # `uniq_rag_document_space_sha256_not_failed`, déjà créée à l'identique par
    # 0010. Ce doublon :
    #   - faisait échouer `migrate` sur une DB neuve (DuplicateObject Postgres),
    #     d'où le workaround `--fake` dans scripts/dev/up.sh ;
    #   - comptait la contrainte 2× dans le state Django, ce qui faisait générer
    #     par makemigrations un 0017 cassé (double RemoveConstraint).
    #
    # On vide donc les opérations. Les DB existantes l'ont déjà appliquée (ou
    # fake-appliquée) : aucun changement physique. Les DB neuves passent 0010
    # puis 0011 (no-op) sans erreur. La modification de la condition de la
    # contrainte (failed -> FAILED) est portée par 0017.
    dependencies = [
        ("rag_core", "0010_document_rag_documen_rag_spa_d89125_idx_and_more"),
    ]

    operations = []
