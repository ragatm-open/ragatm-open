from apps.rag_core.domain.policies import RagPolicy, should_answer

def test_should_answer_uses_text_rank_when_available():
    policy = RagPolicy(min_sources=1, min_text_rank=0.05, min_score=0.9)

    # text_score présent → doit décider via min_text_rank
    assert should_answer([{"text_score": 0.04, "score": 0.99}], policy) is False
    assert should_answer([{"text_score": 0.05, "score": 0.01}], policy) is True
