"""Tests purs du chunker déterministe (apps.rag_core.services.chunking)."""
import pytest

from apps.rag_core.services.chunking import ChunkSpec, chunk_text_deterministic


def _texts(specs):
    return [s.text for s in specs]


class TestChunkTextDeterministic:
    def test_empty_text_returns_no_chunks(self):
        assert chunk_text_deterministic("", source="s", chunk_size=10, overlap=0) == []

    def test_whitespace_only_returns_no_chunks(self):
        assert chunk_text_deterministic("   ", source="s", chunk_size=10, overlap=0) == []

    def test_simple_split_no_overlap(self):
        specs = chunk_text_deterministic("abcdef", source="s", chunk_size=3, overlap=0)
        assert _texts(specs) == ["abc", "def"]
        assert [s.chunk_index for s in specs] == [0, 1]
        assert (specs[0].char_start, specs[0].char_end) == (0, 3)
        assert (specs[1].char_start, specs[1].char_end) == (3, 6)

    def test_overlap_slices(self):
        specs = chunk_text_deterministic("abcdef", source="s", chunk_size=4, overlap=2)
        assert _texts(specs) == ["abcd", "cdef"]

    def test_start_index_and_base_offset(self):
        specs = chunk_text_deterministic(
            "abcdef", source="s", chunk_size=3, overlap=0,
            start_chunk_index=5, base_char_offset=100,
        )
        assert specs[0].chunk_index == 5
        assert specs[0].char_start == 100

    def test_determinism(self):
        a = chunk_text_deterministic("hello world foo", source="s", chunk_size=5, overlap=1)
        b = chunk_text_deterministic("hello world foo", source="s", chunk_size=5, overlap=1)
        assert _texts(a) == _texts(b)

    def test_crlf_normalized(self):
        specs = chunk_text_deterministic("a\r\nb", source="s", chunk_size=100, overlap=0)
        assert "\r" not in specs[0].text

    def test_text_none_raises(self):
        with pytest.raises(ValueError):
            chunk_text_deterministic(None, source="s", chunk_size=10, overlap=0)

    def test_chunk_size_zero_raises(self):
        with pytest.raises(ValueError):
            chunk_text_deterministic("abc", source="s", chunk_size=0, overlap=0)

    def test_overlap_ge_chunk_size_raises(self):
        with pytest.raises(ValueError):
            chunk_text_deterministic("abc", source="s", chunk_size=3, overlap=3)

    def test_chunkspec_shape(self):
        spec = chunk_text_deterministic("abc", source="src", chunk_size=10, overlap=0)[0]
        assert isinstance(spec, ChunkSpec)
        assert spec.source == "src"
        assert spec.page_number is None
