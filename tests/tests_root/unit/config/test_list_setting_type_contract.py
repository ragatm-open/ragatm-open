"""Un réglage `list` au registre doit rester une LISTE jusqu'à son lecteur.

Défaut réel relevé par le lot FORMATS-MSG, corrigé par FORMATS-DWG : trois réglages
déclarés `value_type="list"` au registre (`DOCUMENT_IMAGE_FORMATS`,
`DOCUMENT_PDF_NATIVE_FALLBACK_ENGINES`, `DOCUMENT_VISUAL_ANALYSIS_SOURCES`) étaient
projetés dans `settings/base.py` comme la CHAÎNE brute de l'environnement, et deux
lecteurs faisaient `str(valeur).split(",")`. Le jour où la valeur arrivait sous la forme
que déclare le registre — une liste — `["jpeg", "png"]` devenait `["['jpeg'", " 'png']"]` :
l'allowlist image entière était vidée, et toute image refusée, sans erreur.

Ce test ferme la CLASSE : type déclaré au registre → type projeté dans les settings →
type attendu par le lecteur. Trois contrats distincts, trois assertions distinctes.
"""
import pytest
from django.conf import settings

from apps.rag_core.services.ingest.extraction import config as C
from config import registry

#: Réglages `list` dont le nom est projeté tel quel dans les settings Django.
LIST_SETTINGS = sorted(
    d.name for d in registry.all_definitions()
    if d.value_type == "list" and hasattr(settings, d.name)
)

#: (réglage, lecteur, valeur de test) — le lecteur est celui que le pipeline appelle.
LIST_READERS = [
    ("DOCUMENT_IMAGE_FORMATS", C.image_formats, ["jpeg", "png"]),
    ("DOCUMENT_PDF_NATIVE_FALLBACK_ENGINES", C.pdf_native_fallback_engines, ["pdfminer", "pymupdf"]),
]


def test_la_classe_n_est_pas_vide():
    """Garde-fou : un filtre cassé rendrait les tests paramétrés vides, donc verts."""
    assert "DOCUMENT_IMAGE_FORMATS" in LIST_SETTINGS
    assert len(LIST_SETTINGS) >= 4


@pytest.mark.parametrize("name", LIST_SETTINGS)
def test_un_reglage_list_est_projete_en_liste(name):
    value = getattr(settings, name)
    assert isinstance(value, list), (
        f"{name} est déclaré `list` au registre mais projeté en {type(value).__name__} "
        f"({value!r}) : projeter via `reg({name!r})` dans settings/base.py."
    )


@pytest.mark.parametrize("name", LIST_SETTINGS)
def test_la_projection_egale_le_defaut_du_registre_hors_environnement(name, monkeypatch):
    """Sans variable d'environnement, la projection rend le défaut DÉCLARÉ, pas un autre."""
    from config.settings.from_registry import env_value

    monkeypatch.delenv(name, raising=False)
    assert env_value(name) == registry.get(name).default


@pytest.mark.parametrize("name,reader,value", LIST_READERS, ids=[n for n, _, _ in LIST_READERS])
def test_le_lecteur_consomme_la_forme_canonique(name, reader, value, settings):
    """La forme que projette le registre (liste) est lue élément par élément."""
    setattr(settings, name, value)
    assert reader() == value


@pytest.mark.parametrize("name,reader,value", LIST_READERS, ids=[n for n, _, _ in LIST_READERS])
def test_forme_texte_et_forme_liste_donnent_le_meme_resultat(name, reader, value, settings):
    """`override_settings(X="a,b")` et la projection `["a", "b"]` ne divergent jamais."""
    setattr(settings, name, ",".join(value))
    as_text = reader()
    setattr(settings, name, list(value))
    assert reader() == as_text == value


def test_aucun_element_ne_porte_la_repr_d_une_liste(settings):
    """Régression exacte : `str(["jpeg"]).split(",")` donnait `"['jpeg'"`."""
    settings.DOCUMENT_IMAGE_FORMATS = ["jpeg", "png", "tiff", "webp"]
    formats = C.image_formats()
    assert formats == ["jpeg", "png", "tiff", "webp"]
    assert not any("[" in f or "'" in f for f in formats)


def test_les_sources_visuelles_acceptent_la_liste_projetee(settings):
    settings.DOCUMENT_VISUAL_ANALYSIS_ENABLED = True
    settings.DOCUMENT_VISUAL_ANALYSIS_SOURCES = ["IMAGE", "pdf"]
    assert C.visual_analysis_sources() == frozenset({"image", "pdf"})
