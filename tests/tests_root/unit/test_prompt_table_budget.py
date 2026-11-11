"""Un extrait de CLASSEUR n'a pas le gabarit d'un paragraphe.

Défaut trouvé en campagne terrain, sur le classeur AirWorks : neuf lectures locales sur
onze répondaient « Je ne sais pas » alors que la valeur était bel et bien indexée, et que
la source était affichée à l'utilisateur.

Cause : `RAG_SNIPPET_MAX_CHARS = 1200` rabotait TOUT chunk avant le prompt. Or
l'extracteur Excel produit jusqu'à `DOCUMENT_EXCEL_CHUNK_MAX_CHARS` (4 000) par
conception, et un en-tête hiérarchique de 19 colonnes en consomme à lui seul plus de
1 100. Le modèle recevait donc l'en-tête… et la première ligne. Les deux réglages se
contredisaient depuis E2, et aucun test ne les faisait se rencontrer.

Deux exigences, ici :
  1. un extrait tabulaire garde ses lignes ;
  2. s'il faut vraiment le couper, on coupe sur une frontière de LIGNE et on le DIT —
     un tableau tronqué en silence fait passer une liste partielle pour complète.
"""
from apps.rag_core.domain.retrieval import build_prompt
from apps.rag_core.services.query.prompt_builder import _prepare_selected_for_prompt

# Le gabarit RÉEL : 19 colonnes, en-têtes hiérarchiques. L'en-tête seul dépasse 1 100
# caractères — c'est lui qui saturait à lui tout seul le budget « paragraphe ».
_COLS = ["Security"] + [
    f"{grp} / {lab}"
    for grp in ("Premoney Capitalization", "Anti-Dilution Variables",
                "Postmoney Capitalization")
    for lab in ("Shares Outstanding", "Fully Diluted Shares", "% Total Fully Diluted",
                "Original Issue Price", "Conversion Price (Now)", "Conversion Ratio")
]
_HEADER = "| " + " | ".join(_COLS) + " |"


def _row(name, value):
    return "| " + " | ".join([name, str(value)] + ["0"] * (len(_COLS) - 2)) + " |"


_ROWS = [
    _row("Common", 6234211),
    _row("Series A", 6666916),
    _row("Warrants", 564606),
    _row("Options Outstanding", 1439237),
    _row("Options Available (Current)", 1144158),
]
_TABLE_TEXT = "Feuille : Summary Pro Forma (A3:S13)\n\n" + _HEADER + "\n" + "\n".join(_ROWS)
assert len(_TABLE_TEXT) > 1200, "le cas de test doit dépasser le budget « paragraphe »"


def _excel_chunk(text=_TABLE_TEXT):
    return {
        "text": text,
        "source": {
            "filename": "Pro Forma.xlsx",
            "meta": {"kind": "excel_table", "sheet_name": "Summary Pro Forma",
                     "cell_range": "A3:S13"},
        },
    }


def _pdf_chunk(text="Un paragraphe de prose. " * 200):
    return {"text": text, "source": {"filename": "revue.pdf", "meta": {}}}


class TestTheTabularBudget:
    def test_a_workbook_extract_keeps_its_last_rows(self):
        """Le symptôme exact : « Options Available » disparaissait du prompt."""
        [out] = _prepare_selected_for_prompt(
            [_excel_chunk()], snippet_max_chars=1200, table_max_chars=4000,
        )
        assert "1144158" in out["text"]          # la DERNIÈRE ligne survit
        assert "6234211" in out["text"]          # la première aussi

    def test_the_same_extract_was_amputated_at_the_paragraph_budget(self):
        """Preuve du défaut : au budget « paragraphe », les dernières lignes tombent."""
        [out] = _prepare_selected_for_prompt(
            [_excel_chunk()], snippet_max_chars=1200, table_max_chars=1200,
        )
        assert "1144158" not in out["text"]

    def test_a_prose_chunk_keeps_the_paragraph_budget(self):
        """Le gabarit large ne doit PAS s'appliquer à la prose : le contexte est fini."""
        [out] = _prepare_selected_for_prompt(
            [_pdf_chunk()], snippet_max_chars=1200, table_max_chars=4000,
        )
        assert len(out["text"]) <= 1200

    def test_a_truncated_table_says_so(self):
        """Couper en silence ferait passer une liste partielle pour complète."""
        big = _excel_chunk("\n".join(f"| Ligne {i} | {i * 1000} |" for i in range(400)))
        [out] = _prepare_selected_for_prompt(
            [big], snippet_max_chars=1200, table_max_chars=4000,
        )
        assert "tronqué" in out["text"]
        # Et jamais au milieu d'une ligne : un chiffre amputé serait lu comme une donnée.
        for line in out["text"].splitlines():
            assert line.startswith("| ") or line.startswith("…")


class TestTheCenteredStrategyLeavesTablesAlone:
    """Le contrat tabulaire ne dépend pas de la stratégie de fenêtre.

    La fenêtre centrée (SNIPPET-TRUNCATION-1B) choisit un extrait de prose autour de la
    phrase la plus proche de la question. Appliquée à un tableau, elle produirait des
    colonnes orphelines sans en-tête — lues comme des données. Un tableau se coupe sur
    une frontière de LIGNE, et le dit : c'est vrai dans les deux modes, à l'octet près.
    """

    QUESTION = "Combien d'actions pour Options Available ?"

    def _out(self, chunk, strategy):
        [out] = _prepare_selected_for_prompt(
            [chunk], snippet_max_chars=1200, table_max_chars=4000,
            question=self.QUESTION, snippet_strategy=strategy,
        )
        return out

    def test_a_workbook_extract_is_byte_identical_in_both_modes(self):
        chunk = _excel_chunk("\n".join(f"| Ligne {i} | {i * 1000} |" for i in range(400)))
        assert self._out(chunk, "centered") == self._out(chunk, "head")

    def test_a_pdf_table_chunk_is_byte_identical_in_both_modes(self):
        chunk = {
            "text": "\n".join(f"| Poste {i} | {i * 7} |" for i in range(400)),
            "source": {"filename": "revue.pdf", "meta": {"kind": "table"}},
        }
        assert self._out(chunk, "centered") == self._out(chunk, "head")
        assert "tronqué" in self._out(chunk, "centered")["text"]

    def test_prose_however_does_move(self):
        """Contre-preuve : sans ce test, l'égalité ci-dessus pourrait être triviale."""
        evidence = "Les options disponibles représentent 1144158 actions. "
        chunk = _pdf_chunk("Un paragraphe de prose neutre. " * 60 + evidence + "Suite. " * 60)
        assert evidence.strip() not in self._out(chunk, "head")["text"]
        assert evidence.strip() in self._out(chunk, "centered")["text"]


class TestBuildPromptKeepsTableRows:
    def test_the_workbook_rows_reach_the_prompt(self):
        prompt = build_prompt("Combien pour Options Available ?", [_excel_chunk()])
        assert "1144158" in prompt
        assert "— CLASSEUR" in prompt          # la citation feuille/plage tient toujours

    def test_an_oversized_table_is_cut_on_a_row_boundary_and_declared(self):
        big = _excel_chunk("\n".join(f"| Ligne {i} | {i * 1000} |" for i in range(600)))
        prompt = build_prompt("q", [big])
        assert "extrait tronqué" in prompt
        assert "| Ligne 0 | 0 |" in prompt
