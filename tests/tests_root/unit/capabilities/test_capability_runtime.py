"""parité de configuration des workers, jugée PAR CONSOMMATEUR."""
from __future__ import annotations

import time

from apps.rag_core.services.capabilities import runtime as RT


def _fp(role, digests, *, age=10, sha=None, host="h1"):
    return {"schema_version": RT.SCHEMA_VERSION, "service_role": role, "process_identity": f"{host}:1",
            "deployment_sha": sha if sha is not None else RT.deployment_sha(), "digests": digests,
            "observed_at": time.time() - age}


def test_role_depuis_le_nom_de_noeud():
    assert RT.role_from_hostname("default@abc") == "worker"
    assert RT.role_from_hostname("visual@abc") == "worker_visual"
    assert RT.role_from_hostname("audio@x") == "worker_audio"
    assert RT.role_from_hostname("reindex@x") == "worker_reindex"
    # Compose staging : suffixe d'environnement dans le nom de nœud (constaté au déploiement 30B).
    assert RT.role_from_hostname("audio-staging@abc") == "worker_audio"
    assert RT.role_from_hostname("default-staging@abc") == "worker"
    assert RT.role_from_hostname("visual-staging@abc") == "worker_visual"


def test_empreinte_sans_secret_ni_valeur(settings):
    settings.DOCUMENT_AUDIO_RAGSPACE_IDS = ["11111111-1111-1111-1111-111111111111"]
    fp = RT.build_fingerprint("worker_audio", "h:1")
    assert "11111111" not in str(fp)                     # condensés seulement
    for key in ("environment", "service_role", "process_identity", "deployment_sha",
                "config_schema_version", "consumed_flags_fingerprint", "observed_at"):
        assert key in fp


def test_redis_injoignable_donne_unknown():
    out = RT.consistency(None)
    assert out["status"] == "UNKNOWN" and out["reason"] == "redis_unreachable"


def test_flag_non_consomme_absent_ne_donne_pas_d_alerte(settings):
    """UNRELATED_WORKER_FLAG_MISSING (faux positif 30A : TABULAR_QUERY_ENABLED, AUDIO_GLOBAL)."""
    settings.TABULAR_QUERY_ENABLED = True
    settings.DOCUMENT_AUDIO_GLOBAL_ENABLED = True
    digests = RT.local_digests()
    stale_view = dict(digests)
    stale_view["TABULAR_QUERY_ENABLED"] = RT.value_digest(False)          # worker : défaut (non consommateur)
    stale_view["DOCUMENT_AUDIO_GLOBAL_ENABLED"] = RT.value_digest(False)
    fps = [_fp("worker_analytics", stale_view), _fp("worker_maintenance", stale_view)]
    out = RT.consistency(fps)
    assert out["mismatches"] == []
    assert out["ignored_non_consumer_differences"] >= 2


def test_divergence_reelle_entre_consommateurs(settings):
    """CONFIGURATION_MISMATCH : worker_audio a chargé une autre valeur que le backend."""
    settings.DOCUMENT_AUDIO_GLOBAL_ENABLED = True
    digests = RT.local_digests()
    other = dict(digests)
    other["DOCUMENT_AUDIO_GLOBAL_ENABLED"] = RT.value_digest(False)
    out = RT.consistency([_fp("worker_audio", other), _fp("worker", digests)])
    assert out["status"] == "MISMATCH"
    assert {"flag": "DOCUMENT_AUDIO_GLOBAL_ENABLED", "roles": ["backend", "worker", "worker_audio"]} in out["mismatches"]


def test_worker_perime_n_est_ni_sain_ni_actuel():
    """STALE_WORKER : l'ancienne empreinte n'est pas l'état actuel."""
    digests = RT.local_digests()
    out = RT.consistency([_fp("worker_audio", digests, age=RT.STALE_AFTER_SECONDS + 5)])
    assert out["roles"]["worker_audio"]["state"] == RT.STATE_STALE
    assert out["flags"]["AUDIO_ASR_MODEL_LOGICAL"]["status"] == "UNKNOWN"
    assert "worker_audio" in out["flags"]["AUDIO_ASR_MODEL_LOGICAL"]["unobserved_consumers"]


def test_instance_perimee_remplacee_par_un_pair_frais_est_ignoree():
    digests = RT.local_digests()
    out = RT.consistency([_fp("worker_audio", digests, age=900, host="old"), _fp("worker_audio", digests, host="new")])
    assert out["roles"]["worker_audio"]["state"] == RT.STATE_FRESH and out["roles"]["worker_audio"]["instances"] == 1


def test_sha_de_deploiement_different(settings):
    settings.APP_GIT_SHA = "aaaaaaa"
    out = RT.consistency([_fp("worker", RT.local_digests(), sha="bbbbbbb")])
    assert out["deployment_sha_mismatches"] == ["worker"]


def test_publication_tolere_l_absence_de_redis(settings):
    settings.CELERY_BROKER_URL = "memory://"
    assert RT.publish("worker", "h:1") is False
