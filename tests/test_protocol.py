import json

from fastapi.testclient import TestClient

import main


class FakeChunk:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeLLM:
    async def astream(self, prompt: str):
        for text in ("Hello ", "", "world"):
            yield FakeChunk(text)


client = TestClient(main.app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_chat_streams_sse_increments_and_done(monkeypatch):
    monkeypatch.setattr(main, "llm", FakeLLM())
    response = client.post("/chat", json={"user_id": "u1", "text": "hi"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [e for e in response.text.split("\n\n") if e]
    assert all(e.startswith("data: ") for e in events)
    assert events[-1] == "data: [DONE]"
    chunks = [json.loads(e.removeprefix("data: "))["response"] for e in events[:-1]]
    assert chunks == ["Hello ", "world"]


def test_chat_rejects_malformed_body():
    assert client.post("/chat", json={"text": "hi"}).status_code == 422


class BrokenLLM:
    async def astream(self, prompt: str):
        raise RuntimeError("boom")
        yield


def test_chat_llm_failure_still_ends_with_done(monkeypatch):
    monkeypatch.setattr(main, "llm", BrokenLLM())
    response = client.post("/chat", json={"user_id": "u1", "text": "hi"})

    events = [e for e in response.text.split("\n\n") if e]
    assert "could not reach" in json.loads(events[0].removeprefix("data: "))["response"]
    assert events[-1] == "data: [DONE]"
