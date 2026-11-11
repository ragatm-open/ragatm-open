"""cohérence du registre déclaratif des capacités documentaires.

Ce test est l'anti-péremption de l'écran « Capacités documentaires » : un extracteur, un
détecteur structuré, un flag de capacité ou une porte ajoutés sans classification font échouer
la CI au lieu de laisser l'écran afficher un état périmé.
"""
from __future__ import annotations

import pytest

from apps.rag_core.services.capabilities import registry as R
from apps.rag_core.services.capabilities import resolver as V


def test_identifiants_uniques_et_vocabulaire_controle():
    ids = [c.capability_id for c in R.CAPABILITIES]
    assert len(ids) == len(set(ids)), "identifiant de capacité dupliqué"
    fids = [f.format_id for f in R.FORMATS]
    assert len(fids) == len(set(fids))
    for c in R.CAPABILITIES:
        assert c.category in R.CATEGORIES, c.capability_id
        assert c.implemented in ("YES", "PARTIAL", "NO"), c.capability_id
        assert c.output_indexed in R.INDEX_STATES, c.capability_id
        assert c.qualification_status in R.QUALIFICATION, c.capability_id
        assert c.executing_service in R.ROLES, c.capability_id
        assert set(c.consumers) <= set(R.ROLES), c.capability_id
        assert c.label_fr and c.description_fr, c.capability_id


def test_couverture_des_53_extensions_inventoriees():
    """54 dans l'édition privée ; `.msg` (Outlook) n'existe plus en édition Open."""
    exts = [e for f in R.FORMATS for e in f.extensions]
    assert len(exts) == 53 and len(set(exts)) == 53
    assert ".msg" not in exts


def test_chaque_extracteur_du_registre_est_classe():
    """REGISTRY_EXTRACTOR_COHERENCE : toute clé de _REGISTRY a un format ET une capacité de texte/unités."""
    from apps.rag_core.services.ingest.extraction import _REGISTRY

    by_key: dict = {}
    for f in R.FORMATS:
        if f.registry_key:
            by_key.setdefault(f.registry_key, []).append(f.format_id)
    for key in _REGISTRY:
        assert key in by_key, f"extracteur {key!r} non référencé par un FormatSpec"
        for fmt in by_key[key]:
            assert [c for c in R.CAPABILITIES if c.format_id == fmt], f"format {fmt} sans capacité"
    for f in R.FORMATS:
        if f.registry_key:
            assert f.registry_key in _REGISTRY, f"{f.format_id} référence un extracteur inexistant"


def test_chaque_detecteur_structure_est_classe():
    from apps.rag_core.services.ingest.extraction import mime

    known = {f.registry_key for f in R.FORMATS} | {"audio_transcript"}
    for _prio, name in mime.structured_detectors():
        assert name in known, f"détecteur {name!r} sans format déclaré"


def test_chaque_format_annonce_par_l_api_a_une_fiche():
    from apps.rag_core.services.ingest.extraction import _REGISTRY

    keys = {f.registry_key for f in R.FORMATS}
    assert set(_REGISTRY) <= keys


def test_flags_references_existent_au_registre_de_configuration():
    """REGISTRY_FLAG_COHERENCE : aucun nom de flag inventé."""
    from config import registry as config_registry

    for name in R.referenced_flags():
        assert config_registry.get(name) is not None, f"{name} absent du registre de configuration"
        assert not config_registry.get(name).secret, f"{name} est un secret : jamais dans l'empreinte"


def test_tout_interrupteur_de_capacite_est_classe():
    """Fonction introduite sans classification : un *_ENABLED d'ingestion non référencé ni exclu échoue."""
    from config import registry as config_registry

    referenced = R.referenced_flags() | R.EXCLUDED_FLAGS
    prefixes = ("DOCUMENT_", "STRUCTURED_VISUAL_", "VISUAL_V2_")
    for d in config_registry.all_definitions():
        if d.domain in ("ingestion", "gateway", "tabular", "retrieval", "email") and d.name.endswith("_ENABLED") \
                and d.name.startswith(prefixes) and not d.deprecated:
            assert d.name in referenced, f"{d.name} : flag de capacité non classé (registry.py)"


def test_portes_et_portees_connues_du_resolveur():
    for c in R.CAPABILITIES:
        assert c.gate in V.GATES, f"{c.capability_id} : porte {c.gate!r} inconnue"
        if c.scope:
            assert c.scope in V.SCOPES, f"{c.capability_id} : portée {c.scope!r} inconnue"
    for f in R.FORMATS:
        assert f.gate in V.GATES, f.format_id


def test_dependances_gateway_reliees_a_une_sonde():
    """GATEWAY_DEPENDENCY_COHERENCE."""
    from apps.rag_core.services.capabilities import gateway_health

    for c in R.CAPABILITIES:
        if c.gateway:
            assert c.gateway.service in R.GATEWAY_SERVICES, c.capability_id
            assert c.gateway.service in gateway_health.PROBES, c.capability_id
            assert c.gateway.call_type in R.CALL_TYPES
            assert "://" not in c.gateway.route, "jamais d'hôte dans une route exposée"
    assert set(R.GATEWAY_SERVICES) == set(gateway_health.PROBES)


def test_classification_des_sorties_et_de_la_preuve():
    """OUTPUT_INDEXING_CLASSIFICATION + classes de preuve réelles du moteur."""
    from apps.rag_core.services.query import numeric_evidence as NE

    known = NE.RELIABLE | NE.UNVERIFIED | {"n/a", "classe du format de la pièce"}
    for c in R.CAPABILITIES:
        assert c.numeric_evidence_class in known, f"{c.capability_id} : classe {c.numeric_evidence_class!r} inventée"
        if c.implemented == "NO":
            assert c.output_indexed == R.NOT_PRODUCED, c.capability_id
    by_id = {c.capability_id: c for c in R.CAPABILITIES}
    assert by_id["visual.vision_v2"].output_indexed == R.INTERNAL_ONLY
    assert by_id["visual.curve_shadow"].output_indexed == R.INTERNAL_ONLY
    assert by_id["audio.asr"].numeric_evidence_class == NE.ASR_TRANSCRIPT
    assert by_id["pdf.inline_image.vision"].numeric_evidence_class in NE.UNVERIFIED


@pytest.mark.parametrize("cap_id,local", [
    ("pdf.ocr", True), ("image.ocr", True), ("docx.office_charts", True),
    ("pdf.inline_image.vision", False), ("visual.structx.table", False), ("audio.asr", False),
])
def test_ocr_local_jamais_presente_comme_gateway(cap_id, local):
    by_id = {c.capability_id: c for c in R.CAPABILITIES}
    assert (by_id[cap_id].gateway is None) is local


def test_consommateurs_par_flag():
    cons = R.consumers_by_flag()
    assert cons["TABULAR_QUERY_ENABLED"] == frozenset({"backend"})
    assert "worker_analytics" not in cons["DOCUMENT_AUDIO_GLOBAL_ENABLED"]
    assert "worker_audio" in cons["DOCUMENT_AUDIO_GLOBAL_ENABLED"]
    assert "worker_visual" in cons["DOCUMENT_VISUAL_ANALYSIS_ENABLED"]
