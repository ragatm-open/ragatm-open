"""Réversibilité et innocuité de la migration `chat/0007`.

Exigence de déploiement : la migration doit être **additive, réversible et sans
backfill lourd**, sur une base contenant déjà des messages. Ces tests l'exercent
réellement — aller-retour tête → 0006 → tête — plutôt que de le supposer.

Isolés du reste de la suite : ils restaurent la TÊTE de l'app avant de rendre la
main, y compris en cas d'échec.
"""
import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = [pytest.mark.django_db(transaction=True)]

APP = "chat"
BEFORE = "0006_chatmessage_analysis_id"
#: Migration effectivement sous test (celle dont on vérifie la réversibilité).
UNDER_TEST = "0007_chatmessage_summary_persistence"


def _head() -> str:
    """Dernière migration de l'app, résolue DYNAMIQUEMENT.

    Épingler un numéro ici (``0007``) rendait ces tests faux dès l'ajout de la
    migration suivante : le retour « à la tête » s'arrêtait avant elle, et l'ORM
    lisait ensuite une colonne absente. On revient donc toujours à la vraie tête,
    quelle qu'elle soit — c'est aussi ce que fait un déploiement réel.
    """
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    targets = [name for app, name in executor.loader.graph.leaf_nodes() if app == APP]
    assert targets, "aucune migration de tête pour l'app chat"
    return targets[0]


def _migrate(target):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([(APP, target)])
    executor.loader.build_graph()


@pytest.fixture
def restore_head():
    """Quoi qu'il arrive, la base repart à la TÊTE pour les tests suivants."""
    yield
    _migrate(_head())


def test_migration_is_reversible_and_preserves_existing_messages(
    user, rag_space, restore_head
):
    from apps.chat.models import ChatMessage, ChatThread

    thread = ChatThread.objects.create(user=user, rag_space=rag_space, title="Historique")
    legacy = ChatMessage.objects.create(
        thread=thread, role="assistant", content="Réponse d'avant la migration",
        request_id="rq-legacy",
    )
    legacy_id = legacy.id

    # --- Rollback : les colonnes disparaissent, les messages restent -------
    # Revenir en 0006 dé-applique bien la migration sous test (et toutes celles
    # qui la suivent) : c'est ce que vérifie l'absence de colonnes ci-dessous.
    assert (APP, UNDER_TEST) in MigrationExecutor(connection).loader.graph.nodes
    _migrate(BEFORE)
    with connection.cursor() as cur:
        cur.execute("SELECT content FROM chat_message WHERE id = %s", [str(legacy_id)])
        assert cur.fetchone()[0] == "Réponse d'avant la migration"
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'chat_message'"
        )
        cols = {r[0] for r in cur.fetchall()}
    assert "kind" not in cols and "summary_id" not in cols

    # --- Re-application : additive, sans perte -----------------------------
    _migrate(_head())
    restored = ChatMessage.objects.get(id=legacy_id)
    assert restored.content == "Réponse d'avant la migration"
    # Défauts neutres : un message ancien reste un message standard.
    assert restored.kind == ""
    assert restored.status == ""
    assert restored.document_id is None
    assert restored.summary_id is None


def test_updated_at_backfill_uses_created_at_not_migration_time(
    user, rag_space, restore_head
):
    """Un message ancien n'a pas été « modifié » le jour du déploiement.

    Aligner `updated_at` sur la date de migration fausserait toute lecture
    d'observabilité ultérieure.
    """
    from apps.chat.models import ChatMessage, ChatThread

    thread = ChatThread.objects.create(user=user, rag_space=rag_space, title="T")
    msg = ChatMessage.objects.create(thread=thread, role="user", content="Question")
    msg_id = msg.id

    _migrate(BEFORE)
    _migrate(_head())

    row = ChatMessage.objects.get(id=msg_id)
    assert row.updated_at == row.created_at
