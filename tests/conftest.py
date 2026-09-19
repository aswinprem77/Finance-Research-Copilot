import os

import pytest

from src.retrieval.providers import ENV_PROFILE, LEXICAL


@pytest.fixture(autouse=True)
def lexical_retrieval_by_default(monkeypatch):
    """
    Pin the whole suite to the lexical stack.

    The semantic stack downloads model weights on first use, which would make
    the offline tests network-dependent and slow. Tests that exercise the
    semantic providers inject a fake encoder instead, and the one test that
    loads real weights opts in explicitly (see test_providers.py).
    """
    monkeypatch.setenv(ENV_PROFILE, LEXICAL)
    # A stray HF token or offline flag in the developer's environment should not
    # change what the suite does.
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("RERANKER_MODEL", raising=False)


def pytest_report_header(config):
    return f"retrieval profile under test: {os.getenv(ENV_PROFILE, LEXICAL)}"
