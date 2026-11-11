from apps.rag_core.domain.policies import RagPolicy, should_answer

def test_should_answer_respects_min_score_when_no_text_signal():
    policy = RagPolicy(min_sources=1, min_score=0.5, min_text_rank=0.05)

    # Pas de signal FTS -> on tombe sur la règle min_score (vector-only)
    chunks = [{"score": 0.49, "text_score": 0.0, "vector_score": 0.49}]
    assert should_answer(chunks, policy) is False

    chunks = [{"score": 0.50, "text_score": 0.0, "vector_score": 0.50}]
    assert should_answer(chunks, policy) is True
