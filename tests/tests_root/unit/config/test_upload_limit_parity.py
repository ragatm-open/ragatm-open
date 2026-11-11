"""La limite d'upload ADMISE par l'API est celle qu'applique l'EXTRACTION côté worker.

Défaut constaté en staging le 21/09/2026 : `worker_visual` tournait une image antérieure
au passage à 50 Mio ; l'API (50 Mio) acceptait des PDF de 29 et 35 Mo que le worker
refusait ensuite en `file_too_large` (limite 25 Mio). Deux causes, deux verrous :
  - l'image périmée : `assert-revision.sh` interroge désormais `worker_visual`
    (cf. tests/tests_root/deploy/test_release_revision_chain.py) ;
  - la dérive de CODE : les deux lecteurs de la limite portaient chacun un repli codé en
    dur à 25, tandis que le registre déclarait 50. Ce fichier les confronte.
"""
from apps.rag_core.services.ingest import limits
from apps.rag_core.services.ingest.extraction import config as xcfg
from config import registry

MIB = 1024 * 1024


def test_admission_http_et_extraction_worker_appliquent_la_meme_limite():
    assert limits.max_file_size_bytes() == xcfg.max_upload_size_bytes()


def test_la_limite_effective_est_celle_du_registre():
    assert limits.max_file_size_bytes() == int(registry.require("UPLOAD_MAX_FILE_SIZE_MB").default * MIB)


def test_sans_reglage_les_deux_lecteurs_retombent_sur_le_defaut_du_registre(settings):
    """Un réglage absent ne doit jamais faire réapparaître une ancienne valeur codée en dur."""
    del settings.UPLOAD_MAX_FILE_SIZE_MB
    del settings.DOCUMENT_MAX_UPLOAD_SIZE_MB
    attendu = int(registry.require("UPLOAD_MAX_FILE_SIZE_MB").default * MIB)
    assert limits.max_file_size_bytes() == attendu
    assert xcfg.max_upload_size_bytes() == attendu


def test_un_fichier_de_29_mo_accepte_par_l_api_n_est_pas_refuse_par_l_extraction():
    """Le cas réel : `E3 - RAAT…pdf` (29 789 435 octets) accepté puis rejeté."""
    taille = 29_789_435
    limits.check_file_size(taille)  # admission : ne lève pas
    assert taille <= xcfg.max_upload_size_bytes()
