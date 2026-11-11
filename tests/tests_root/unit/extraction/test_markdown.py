"""Tests extracteur Markdown."""
from apps.rag_core.services.ingest.extraction import extract_document
from apps.rag_core.services.ingest.extraction.base import BlockType

MD = """---
title: Doc de test
author: alice
---
# Titre 1

Un paragraphe normal avec un lien [exemple](https://example.com).

## Sous-titre

- item a
- item b

```python
print("ne doit pas être mélangé au paragraphe")
```

| col1 | col2 |
|------|------|
| a    | b    |

Paragraphe avec <script>alert('xss')</script> et <b>gras</b> inline.
"""


def _types(res):
    return [b.type for b in res.blocks]


class TestMarkdown:
    def test_nominal(self):
        r = extract_document(MD.encode(), filename="d.md")
        assert r.status.value in ("success", "partial")
        assert r.metadata["format"] == "markdown"

    def test_titles_preserved(self):
        r = extract_document(MD.encode(), filename="d.md")
        titles = [b for b in r.blocks if b.type == BlockType.TITLE]
        assert any(b.level == 1 and "Titre 1" in b.text for b in titles)
        assert any(b.level == 2 for b in titles)
        assert r.metadata["sections"] >= 2

    def test_code_block_is_distinct(self):
        r = extract_document(MD.encode(), filename="d.md")
        code = [b for b in r.blocks if b.type == BlockType.CODE]
        assert code and "print(" in code[0].text
        # le code ne doit pas être fusionné dans un paragraphe
        for b in r.blocks:
            if b.type == BlockType.PARAGRAPH:
                assert "print(" not in b.text

    def test_table_and_list(self):
        r = extract_document(MD.encode(), filename="d.md")
        assert any(b.type == BlockType.TABLE for b in r.blocks)
        assert any(b.type == BlockType.LIST for b in r.blocks)

    def test_front_matter_not_in_content(self):
        r = extract_document(MD.encode(), filename="d.md")
        assert "author: alice" not in r.text
        assert "front_matter" in r.metadata
        assert "front_matter_extracted" in r.warnings

    def test_inline_html_neutralized(self):
        r = extract_document(MD.encode(), filename="d.md")
        assert "<script>" not in r.text
        assert "alert(" not in r.text
        assert "html_inline_neutralized" in r.warnings
        # le texte visible "gras" est conservé
        assert "gras" in r.text

    def test_truncation_limit(self, settings):
        settings.DOCUMENT_MARKDOWN_MAX_CHARS = 20
        r = extract_document(("# x\n" + "a" * 1000).encode(), filename="d.md")
        assert "truncated_max_chars" in r.warnings
        assert r.partial is True
