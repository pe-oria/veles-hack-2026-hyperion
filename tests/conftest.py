import pytest

from hyperion import llm


@pytest.fixture(autouse=True)
def configured_key(monkeypatch):
    """Tests never call the real server and must pass on a machine without a .env."""
    monkeypatch.setattr(llm, "API_KEY", "sk-test")
