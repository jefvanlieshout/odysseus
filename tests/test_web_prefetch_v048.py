from types import SimpleNamespace

from routes.chat_helpers import _legacy_web_prefetch_enabled
from src.chat_processor import ChatProcessor


def test_false_string_does_not_enable_legacy_web_prefetch():
    assert _legacy_web_prefetch_enabled("false", agent_mode=False) is False


def test_explicit_true_enables_prefetch_only_in_plain_chat():
    assert _legacy_web_prefetch_enabled("true", agent_mode=False) is True
    assert _legacy_web_prefetch_enabled(True, agent_mode=False) is True


def test_agent_mode_never_uses_legacy_web_prefetch():
    assert _legacy_web_prefetch_enabled("true", agent_mode=True) is False
    assert _legacy_web_prefetch_enabled(True, agent_mode=True) is False


def test_agent_mode_chat_processor_does_not_search_even_if_use_web_true(monkeypatch):
    def forbidden_search(*args, **kwargs):
        raise AssertionError("legacy comprehensive_web_search must not run in agent mode")

    monkeypatch.setattr(
        "src.chat_processor.comprehensive_web_search",
        forbidden_search,
    )

    processor = ChatProcessor(
        memory_manager=None,
        personal_docs_manager=None,
    )

    session = SimpleNamespace(
        endpoint_url="http://localhost:8000/v1/chat/completions",
        model="Qwen/Qwen3.8-27B",
        headers={},
    )

    preface, rag_sources, web_sources = processor.build_context_preface(
        message="very nice! now we also have the approve or deny working!",
        session=session,
        use_web=True,
        use_rag=False,
        use_memory=False,
        agent_mode=True,
        use_skills=False,
    )

    assert web_sources == []
