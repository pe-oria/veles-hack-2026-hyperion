import json

import pytest
from fastapi.testclient import TestClient

import main
from hyperion import llm, prompts, router, session
from hyperion.router import Route

client = TestClient(main.app)


class FakeChunk:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeLLM:
    """Records the messages it was asked to answer."""

    def __init__(self) -> None:
        self.calls: list[list] = []

    async def astream(self, messages):
        self.calls.append(list(messages))
        for text in ("Hello ", "", "world"):
            yield FakeChunk(text)


@pytest.fixture(autouse=True)
def fresh_sessions():
    session._sessions.clear()


@pytest.fixture
def fake_llm(monkeypatch) -> FakeLLM:
    fake = FakeLLM()
    monkeypatch.setattr(llm, "chat_llm", fake)
    return fake


def set_route(monkeypatch, **fields) -> None:
    async def fake_route(text, sess):
        return Route(**fields)

    monkeypatch.setattr(router, "route", fake_route)


def post(text: str, user_id: str = "u1") -> list[str]:
    response = client.post("/chat", json={"user_id": user_id, "text": text})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [e for e in response.text.split("\n\n") if e]
    assert all(e.startswith("data: ") for e in events)
    assert events[-1] == "data: [DONE]"
    return [json.loads(e.removeprefix("data: "))["response"] for e in events[:-1]]


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_question_streams_increments(monkeypatch, fake_llm):
    set_route(monkeypatch, intent="question")
    assert post("What is HyperAI?") == ["Hello ", "world"]


def test_off_topic_is_refused_without_calling_the_llm(monkeypatch, fake_llm):
    set_route(monkeypatch, intent="off_topic")
    assert post("What is the weather today?") == [prompts.REFUSAL]
    assert fake_llm.calls == []


def test_history_is_kept_per_user(monkeypatch, fake_llm):
    set_route(monkeypatch, intent="question")
    post("first question", user_id="alice")
    post("second question", user_id="alice")
    post("other user", user_id="bob")

    alice_second, bob_first = fake_llm.calls[1], fake_llm.calls[2]
    assert ("user", "first question") in alice_second
    assert ("assistant", "Hello world") in alice_second
    assert alice_second[-1] == ("user", "second question")
    assert ("user", "first question") not in bob_first


def test_failure_still_ends_with_done_and_is_not_remembered(monkeypatch):
    async def broken_route(text, sess):
        raise RuntimeError("boom")

    monkeypatch.setattr(router, "route", broken_route)
    assert post("hi") == [prompts.LLM_ERROR]
    assert session.get_session("u1").history == []


def test_chat_rejects_malformed_body():
    assert client.post("/chat", json={"text": "hi"}).status_code == 422
