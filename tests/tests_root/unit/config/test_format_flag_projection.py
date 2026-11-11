"""Un réglage déclaré au registre doit être PROJETÉ dans les settings Django.

Défaut réel trouvé par le lot FORMATS-MSG : `DOCUMENT_MSG_ENABLED` était déclaré au
registre, documenté et écrit dans `.env.staging` — mais jamais projeté dans
`config/settings/base.py`. Or `extraction/config.py` lit les réglages par
`getattr(settings, NOM, defaut)`. Le flag valait donc `False` quoi qu'on écrive dans
l'environnement : le format était INACTIVABLE, en silence, sur tous les environnements.

Rien ne le signalait. Le registre listait la variable, la doc expliquait comment
l'activer, `.env.example` la proposait — et l'activer ne faisait rien. Ce test ferme
la CLASSE de défaut, pas l'instance : toute famille de format doit voir son
interrupteur atteindre réellement le code qui le lit.
"""
import pytest
from django.conf import settings

from apps.rag_core.services.ingest.extraction import config as C

#: (réglage, lecteur) — le lecteur est la fonction que le pipeline appelle vraiment.
FORMAT_GATES = [
    ("DOCUMENT_MSG_ENABLED", C.msg_enabled),
    ("DOCUMENT_EML_ENABLED", C.eml_enabled),
    ("DOCUMENT_EMAIL_ATTACHMENT_INGEST_ENABLED", C.email_attachment_ingest_enabled),
    ("DOCUMENT_EMAIL_GLOBAL_ENABLED", C.email_global_enabled),
    ("DOCUMENT_AUDIO_INGEST_ENABLED", C.audio_ingest_enabled),
    ("DOCUMENT_AUDIO_GLOBAL_ENABLED", C.audio_global_enabled),
    ("DOCUMENT_XML_ENABLED", C.xml_enabled),
    ("DOCUMENT_DXF_ENABLED", C.dxf_enabled),
    ("DOCUMENT_IFC_ENABLED", C.ifc_enabled),
    ("DOCUMENT_DWG_ENABLED", C.dwg_enabled),
    ("DOCUMENT_EXCEL_ENABLED", C.excel_enabled),
    ("DOCUMENT_PPTX_ENABLED", C.pptx_enabled),
    ("DOCUMENT_IMAGE_INGEST_ENABLED", C.image_ingest_enabled),
]


@pytest.mark.parametrize("name,_reader", FORMAT_GATES, ids=[n for n, _ in FORMAT_GATES])
def test_le_reglage_existe_dans_les_settings(name, _reader):
    """`getattr(settings, name, defaut)` ne doit PAS retomber sur son défaut."""
    assert hasattr(settings, name), (
        f"{name} n'est pas projeté dans les settings Django : le lecteur retombera "
        f"toujours sur son défaut et la variable d'environnement sera sans effet."
    )


@pytest.mark.parametrize("name,reader", FORMAT_GATES, ids=[n for n, _ in FORMAT_GATES])
def test_le_reglage_gouverne_vraiment_son_lecteur(name, reader, settings):
    """Basculer le réglage doit changer ce que LIT le pipeline, dans les deux sens.

    `hasattr` ne suffit pas : un réglage peut exister et n'être lu par personne. Ce
    test relie l'interrupteur à son lecteur réel.
    """
    setattr(settings, name, True)
    assert reader() is True, f"{name}=True n'ouvre pas {reader.__name__}()"
    setattr(settings, name, False)
    assert reader() is False, f"{name}=False ne ferme pas {reader.__name__}()"
