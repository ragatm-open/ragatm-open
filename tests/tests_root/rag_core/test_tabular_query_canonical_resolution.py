"""Résolution documentaire canonique du harnais : `content_sha256` fait foi.

Tant que le runner résolvait par nom de fichier, un dataset pourtant correctement figé
pouvait s'exécuter contre le mauvais classeur au premier doublon, renommage ou collision
de nom. La propriété que la convergence vient de garantir — un cas désigne un document
et un seul — n'était pas tenue au moment de l'exécution.

Verdict encodé ici :

    content_sha256      = identité
    filename            = information de diagnostic
    document_id runtime = résolution locale

Fixtures SYNTHÉTIQUES. Aucun corpus réel, aucun holdout.
"""
from __future__ import annotations

import pytest

from apps.rag_core.benchmark import tabular_query_dataset as ds
from apps.rag_core.benchmark import tabular_query_runner as run


def _doc(filename: str, payload: bytes) -> ds.CorpusDocument:
    return ds.CorpusDocument(
        filename=filename, data=payload, is_csv=True, sheet_name="Feuille1",
        header_range="A1:B1", data_range="A2:B3", columns=("K", "V"),
        rows=({"K": "a", "V": 1},))


def _corpus(*documents: ds.CorpusDocument) -> ds.Corpus:
    corpus = ds.Corpus(directory=None)
    for d in documents:
        corpus.documents[d.filename] = d
    return corpus


def _case(document="a.csv", sha=""):
    return {"id": "c1", "question": "Combien de lignes ?", "document": document,
            "content_sha256": sha}


# --------------------------------------------------------------------------- #
# L'identité est le hachage, pas le nom
# --------------------------------------------------------------------------- #
def test_content_sha256_identifies_the_document():
    doc = _doc("a.csv", b"colonnes\n1\n")
    corpus = _corpus(doc)
    resolved, trace = run.resolve_document_canonically(
        _case(sha=doc.content_sha256), corpus)
    assert resolved is doc
    assert trace["resolution"] == "content_sha256"
    assert trace["resolved_content_sha256"] == doc.content_sha256


def test_two_documents_sharing_a_filename_are_distinguished_by_their_sha():
    """Le cas décisif : même nom, contenus différents. Le nom ne peut pas trancher."""
    a = _doc("rapport.csv", b"contenu A\n")
    b = _doc("rapport.csv", b"contenu B\n")
    assert a.content_sha256 != b.content_sha256
    corpus = ds.Corpus(directory=None)
    corpus.documents["rapport-a.csv"] = a          # clés distinctes, filename identique
    corpus.documents["rapport-b.csv"] = b
    resolved, _ = run.resolve_document_canonically(
        _case(document="rapport.csv", sha=b.content_sha256), corpus)
    assert resolved is b                            # jamais le premier homonyme


def test_a_renamed_document_still_resolves_and_the_rename_is_visible():
    """Un renommage ne doit pas casser un dataset figé — mais doit se voir."""
    doc = _doc("nouveau-nom.csv", b"contenu stable\n")
    corpus = _corpus(doc)
    resolved, trace = run.resolve_document_canonically(
        _case(document="ancien-nom.csv", sha=doc.content_sha256), corpus)
    assert resolved is doc
    assert trace["renamed"] is True
    assert trace["expected_document"] == "ancien-nom.csv"
    assert trace["resolved_document"] == "nouveau-nom.csv"


# --------------------------------------------------------------------------- #
# Refus — jamais de repli silencieux
# --------------------------------------------------------------------------- #
def test_a_missing_sha_is_refused_not_resolved_by_name():
    """Sans hachage, l'identité n'est pas vérifiable : on refuse plutôt que deviner."""
    corpus = _corpus(_doc("a.csv", b"x\n"))
    with pytest.raises(run.DocumentResolutionError) as e:
        run.resolve_document_canonically(_case(sha=""), corpus)
    assert e.value.code == run.ERR_MISSING_CONTENT_SHA256


def test_an_unknown_sha_is_refused_even_when_the_filename_matches():
    """Le nom correspond, le contenu non : le repli sur le nom est exactement le bug."""
    corpus = _corpus(_doc("a.csv", b"contenu reel\n"))
    with pytest.raises(run.DocumentResolutionError) as e:
        run.resolve_document_canonically(_case(document="a.csv", sha="b" * 64), corpus)
    assert e.value.code == run.ERR_DOCUMENT_NOT_FOUND


def test_duplicate_sha_is_refused_rather_than_arbitrated():
    """Deux documents au contenu identique : lequel le dataset visait-il ? Indécidable."""
    payload = b"contenu partage\n"
    corpus = _corpus(_doc("copie-1.csv", payload), _doc("copie-2.csv", payload))
    sha = corpus.documents["copie-1.csv"].content_sha256
    with pytest.raises(run.DocumentResolutionError) as e:
        run.resolve_document_canonically(_case(document="copie-1.csv", sha=sha), corpus)
    assert e.value.code == run.ERR_DUPLICATE_CONTENT_SHA256
    assert "copie-1.csv" in str(e.value) and "copie-2.csv" in str(e.value)


def test_no_silent_fallback_to_the_filename_exists():
    """Garde structurelle : le nom n'entre jamais dans la SÉLECTION.

    Chercher la sous-chaîne « filename » serait trop grossier : le nom figure
    légitimement dans le message d'erreur des doublons et dans la trace de diagnostic,
    ce que ce lot autorise explicitement. On vise donc les formes qui *sélectionnent*
    par le nom.
    """
    import inspect
    source = inspect.getsource(run.resolve_document_canonically)
    for selection_by_name in ("documents.get(", "documents[", "d.filename ==",
                              ".filename.lower()", "filename in question"):
        assert selection_by_name not in source, \
            f"la résolution sélectionne par le nom : {selection_by_name!r}"
    assert "by_content_sha256" in source, "la sélection doit passer par le hachage"


# --------------------------------------------------------------------------- #
# Identifiant runtime
# --------------------------------------------------------------------------- #
def test_runtime_document_id_derives_from_the_sha_not_the_filename():
    a = _doc("meme-nom.csv", b"A\n")
    b = _doc("meme-nom.csv", b"B\n")
    assert run.document_id_for(a.content_sha256) != run.document_id_for(b.content_sha256)


def test_runtime_document_id_survives_a_rename():
    payload = b"contenu stable\n"
    before, after = _doc("avant.csv", payload), _doc("apres.csv", payload)
    assert run.document_id_for(before.content_sha256) == run.document_id_for(
        after.content_sha256)


def test_runtime_document_id_is_stable_and_case_insensitive_on_the_sha():
    sha = "A" * 64
    assert run.document_id_for(sha) == run.document_id_for(sha.lower())


# --------------------------------------------------------------------------- #
# Compatibilité du dataset synthétique
# --------------------------------------------------------------------------- #
def test_every_generated_case_naming_a_document_carries_its_sha(tmp_path):
    corpus = ds.build_corpus(tmp_path)
    cases = ds.build_cases(corpus)
    assert cases
    for case in cases:
        if case["document"]:
            expected = corpus.documents[case["document"]].content_sha256
            assert case["content_sha256"] == expected, case["id"]


def test_document_selection_cases_keep_no_sha_so_they_still_measure_selection(tmp_path):
    """Ces cas mesurent la sélection PAR LE MOTEUR : leur donner un sha la court-circuiterait."""
    cases = ds.build_cases(ds.build_corpus(tmp_path))
    selection = [c for c in cases if not c["document"]]
    assert selection, "aucun cas de sélection de document"
    for case in selection:
        assert case["content_sha256"] == ""


def test_the_runner_executes_the_document_the_case_names(tmp_path):
    """Preuve de bout en bout : le document observé est bien celui du hachage attendu."""
    corpus = ds.build_corpus(tmp_path)
    cases = [c for c in ds.build_cases(corpus) if c["document"]]
    for case in cases[:5]:
        obs = run.run_once(case, corpus)
        assert obs.document_resolution.get("resolution") == "content_sha256"
        assert (obs.document_resolution.get("resolved_content_sha256")
                == case["content_sha256"])


def test_a_case_pointing_at_an_absent_document_fails_instead_of_running(tmp_path):
    """Un dataset désynchronisé du corpus doit échouer bruyamment, pas s'exécuter."""
    corpus = ds.build_corpus(tmp_path)
    case = next(c for c in ds.build_cases(corpus) if c["document"])
    case = {**case, "content_sha256": "f" * 64}
    obs = run.run_once(case, corpus)
    assert obs.status == ds.STATUS_ERROR
    assert obs.code == run.ERR_DOCUMENT_NOT_FOUND
    assert obs.document_resolution["resolved_document"] == ""


# --------------------------------------------------------------------------- #
# Invariant du schéma : le type de cas détermine la voie de résolution
# --------------------------------------------------------------------------- #
def test_a_named_document_without_a_sha_is_refused():
    """Sans hachage, ce cas serait résolu par son nom — la régression qu'on ferme."""
    with pytest.raises(ds.DocumentIdentityError) as e:
        ds.validate_document_identity([_case(document="a.csv", sha="")])
    assert e.value.code == ds.ERR_DOCUMENT_WITHOUT_SHA


def test_a_sha_without_a_document_is_refused():
    """Un cas de sélection documentaire porteur d'un hachage ne mesurerait plus rien."""
    with pytest.raises(ds.DocumentIdentityError) as e:
        ds.validate_document_identity([_case(document="", sha="a" * 64)])
    assert e.value.code == ds.ERR_SHA_WITHOUT_DOCUMENT


@pytest.mark.parametrize("document,sha", [("a.csv", "a" * 64), ("", "")])
def test_coherent_states_are_accepted(document, sha):
    ds.validate_document_identity([_case(document=document, sha=sha)])


def test_the_generated_dataset_satisfies_the_invariant(tmp_path):
    ds.validate_document_identity(ds.build_cases(ds.build_corpus(tmp_path)))


def test_an_incoherent_case_becomes_a_status_not_an_exception(tmp_path):
    """Le runner ne lève jamais : un cas mal formé devient un statut d'erreur."""
    corpus = ds.build_corpus(tmp_path)
    case = next(c for c in ds.build_cases(corpus) if c["document"])
    obs = run.run_once({**case, "content_sha256": ""}, corpus)
    assert obs.status == ds.STATUS_ERROR
    assert obs.code == ds.ERR_DOCUMENT_WITHOUT_SHA
