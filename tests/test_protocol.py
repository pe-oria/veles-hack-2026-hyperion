import json

import pytest
from fastapi.testclient import TestClient

import main
from hyperion import llm, prompts, rag, router, session
from hyperion.rag import Chunk
from hyperion.router import Route

client = TestClient(main.app)


class FakeChunk:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeLLM:
    """Records the messages it was asked to answer."""

    def __init__(self, pieces=("Hello ", "", "world")) -> None:
        self.calls: list[list] = []
        self.pieces = pieces

    async def astream(self, messages):
        self.calls.append(list(messages))
        for text in self.pieces:
            yield FakeChunk(text)


def text_of(pieces: list[str]) -> str:
    return "".join(pieces)


DOC = Chunk(title="Intro Doc", section="About", text="HyperAI is an EU project.")


@pytest.fixture(autouse=True)
def fresh_sessions():
    session._sessions.clear()


@pytest.fixture(autouse=True)
def fake_search(monkeypatch) -> dict:
    """Stand-in for the embedding index; tests set state["score"] / state["fail"]."""
    state = {"score": 0.8, "fail": False, "queries": []}

    async def search(queries, k=rag.TOP_K):
        if state["fail"]:
            raise RuntimeError("embedding server down")
        state["queries"].append(list(queries))
        return [[(DOC, state["score"])] for _ in queries]

    async def get_index():
        return rag.Index([DOC], None)

    monkeypatch.setattr(rag, "search", search)
    monkeypatch.setattr(rag, "get_index", get_index)
    return state


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


def test_question_is_grounded_and_cites_sources(monkeypatch, fake_llm):
    set_route(monkeypatch, intent="question")
    assert text_of(post("What is HyperAI?")) == "Hello world\n\nSources: Intro Doc"
    prompt = fake_llm.calls[0][-1][1]
    assert "### Intro Doc - About\nHyperAI is an EU project." in prompt
    assert prompt.endswith("Question: What is HyperAI?")


def test_model_written_sources_line_is_dropped(monkeypatch):
    fake = FakeLLM(pieces=("They manage devices.\n\nSour", "ces: Made Up Doc, Another"))
    monkeypatch.setattr(llm, "chat_llm", fake)
    set_route(monkeypatch, intent="question")
    assert text_of(post("What are Open Connectors?")) == "They manage devices.\n\nSources: Intro Doc"


def test_streaming_stays_incremental(monkeypatch):
    fake = FakeLLM(pieces=("The connectors manage ", "edge devices as ", "cloud nodes."))
    monkeypatch.setattr(llm, "chat_llm", fake)
    set_route(monkeypatch, intent="question")
    pieces = post("What are Open Connectors?")
    assert len(pieces) >= 3
    assert text_of(pieces) == "The connectors manage edge devices as cloud nodes.\n\nSources: Intro Doc"


def test_markdown_is_stripped_because_the_ide_chat_shows_raw_text(monkeypatch):
    fake = FakeLLM(pieces=("The settings are:\n* *", "*Image*", "*: `nginx`\n", "* **Port**: 80"))
    monkeypatch.setattr(llm, "chat_llm", fake)
    set_route(monkeypatch, intent="question", about_conversation=True)
    assert text_of(post("what did you make?")) == "The settings are:\n- Image: nginx\n- Port: 80"


def test_empty_message_gets_a_hint_without_any_model_call(monkeypatch, fake_llm):
    async def route(text, sess):
        raise AssertionError("the router must not be called")

    monkeypatch.setattr(router, "route", route)
    assert post("   ") == [prompts.EMPTY_MESSAGE]


def test_stream_is_not_buffered_by_proxies(monkeypatch, fake_llm):
    set_route(monkeypatch, intent="smalltalk")
    response = client.post("/chat", json={"user_id": "u1", "text": "hi"})
    assert response.headers["cache-control"] == "no-cache" and response.headers["x-accel-buffering"] == "no"


def test_weak_matches_are_neither_shown_nor_cited(monkeypatch, fake_llm, fake_search):
    set_route(monkeypatch, intent="question")
    fake_search["score"] = 0.56
    assert text_of(post("What is a HyperAI flux capacitor?")) == "Hello world"
    assert "(none matched)" in fake_llm.calls[0][-1][1]


def test_conversation_question_uses_history_only(monkeypatch, fake_llm, fake_search):
    set_route(monkeypatch, intent="question")
    post("What is HyperAI?")
    set_route(monkeypatch, intent="question", about_conversation=True)
    searches = len(fake_search["queries"])
    assert text_of(post("What was my first question?")) == "Hello world"

    system, *history, latest = fake_llm.calls[1]
    assert system == ("system", prompts.ANSWER_CONVERSATION_SYSTEM)
    assert history == [("user", "What is HyperAI?"), ("assistant", "Hello world")]
    assert latest == ("user", "What was my first question?")
    assert len(fake_search["queries"]) == searches  # no retrieval, no excerpts, no sources


def test_question_survives_retrieval_outage(monkeypatch, fake_llm, fake_search):
    set_route(monkeypatch, intent="question")
    fake_search["fail"] = True
    assert text_of(post("What is HyperAI?")) == "Hello world"


def test_off_topic_is_refused_without_calling_the_llm(monkeypatch, fake_llm, fake_search):
    set_route(monkeypatch, intent="off_topic")
    fake_search["score"] = 0.5
    assert post("What is the weather today?") == [prompts.REFUSAL]
    assert fake_llm.calls == []


def test_question_far_from_the_knowledge_base_is_refused(monkeypatch, fake_llm, fake_search):
    set_route(monkeypatch, intent="question")
    fake_search["score"] = 0.5
    assert post("How do I cook lasagna?") == [prompts.REFUSAL]


def test_off_topic_verdict_is_rescued_by_high_similarity(monkeypatch, fake_llm, fake_search):
    set_route(monkeypatch, intent="off_topic")
    fake_search["score"] = 0.8
    assert text_of(post("How are edge gadgets registered?")).startswith("Hello world")


def test_history_is_kept_per_user(monkeypatch, fake_llm):
    set_route(monkeypatch, intent="question")
    post("first question", user_id="alice")
    post("second question", user_id="alice")
    post("other user", user_id="bob")

    alice_second, bob_first = fake_llm.calls[1], fake_llm.calls[2]
    # history holds the raw question, not the prompt with excerpts
    assert ("user", "first question") in alice_second
    assert ("assistant", "Hello world") in alice_second
    assert alice_second[-1][1].endswith("Question: second question")
    assert ("user", "first question") not in bob_first


def test_follow_up_is_retrieved_with_the_previous_question(monkeypatch, fake_llm, fake_search):
    set_route(monkeypatch, intent="question")
    post("What are Open Connectors?")
    post("who develops them?")
    # guardrail scores the raw message; retrieval uses the history-aware query
    assert fake_search["queries"][1] == ["who develops them?", "What are Open Connectors? who develops them?"]


def test_failure_still_ends_with_done_and_is_not_remembered(monkeypatch):
    async def broken_route(text, sess):
        raise RuntimeError("boom")

    monkeypatch.setattr(router, "route", broken_route)
    assert post("hi") == [prompts.LLM_ERROR]
    assert session.get_session("u1").history == []


def test_actions_are_streamed_as_their_own_events_and_kept_out_of_history(monkeypatch):
    async def handle(route, text, sess):
        yield "Creating `a.yaml`."
        yield {"action": "create_file", "path": "a.yaml", "content": "x: 1\n"}
        yield " Done."

    monkeypatch.setattr(main.fileops, "handle", handle)
    set_route(monkeypatch, intent="create_file")
    response = client.post("/chat", json={"user_id": "u1", "text": "make a.yaml"})
    events = [json.loads(e.removeprefix("data: ")) for e in response.text.split("\n\n") if e and "[DONE]" not in e]
    assert events == [
        {"response": "Creating `a.yaml`."},
        {"action": "create_file", "path": "a.yaml", "content": "x: 1\n"},
        {"response": " Done."},
    ]
    assert response.text.endswith("data: [DONE]\n\n")
    assert session.get_session("u1").history[-1] == ("assistant", "Creating `a.yaml`. Done.")


def test_missing_api_key_is_reported_clearly_without_calling_anything(monkeypatch, fake_llm):
    async def route(text, sess):
        raise AssertionError("no model call is possible without a key")

    monkeypatch.setattr(llm, "API_KEY", "")
    monkeypatch.setattr(router, "route", route)
    assert post("What is HyperAI?") == [prompts.MISSING_KEY]
    assert "API_KEY" in prompts.MISSING_KEY and fake_llm.calls == []
    assert client.get("/health").status_code == 200  # the service itself stays up


def test_rejected_api_key_is_reported_clearly(monkeypatch):
    import httpx
    import openai

    async def rejected(text, sess):
        response = httpx.Response(401, request=httpx.Request("POST", "https://legion1.di.uoa.gr/v1/chat/completions"))
        raise openai.AuthenticationError("bad key", response=response, body=None)

    monkeypatch.setattr(router, "route", rejected)
    assert post("What is HyperAI?") == [prompts.REJECTED_KEY]
    assert session.get_session("u1").history == []


def test_error_after_partial_output_starts_on_its_own_line(monkeypatch):
    class Dies:
        async def astream(self, messages):
            yield FakeChunk("The connectors manage edge devices")
            raise RuntimeError("connection dropped")

    monkeypatch.setattr(llm, "chat_llm", Dies())
    set_route(monkeypatch, intent="question")
    reply = text_of(post("What are Open Connectors?"))
    assert reply.endswith("\n\n" + prompts.LLM_ERROR) and reply.startswith("The connectors")


def test_naming_the_image_completes_the_waiting_create(monkeypatch):
    from hyperion import ide, yamlgen

    async def no_params(text):
        return {}

    async def nothing_there(path):
        return None

    async def saved(path, content, timeout=None):
        return True

    async def valid(path):
        return {"path": path, "type": "native", "valid": True, "errors": [], "warnings": []}

    monkeypatch.setattr(yamlgen, "llm_params", no_params)
    monkeypatch.setattr(ide, "find", nothing_there)
    monkeypatch.setattr(ide, "wait_for_content", saved)
    monkeypatch.setattr(ide, "validate", valid)
    set_route(monkeypatch, intent="create_file", app_kind="native")

    assert post("create a deployment yaml for my web service") == [prompts.ASK_IMAGE]
    response = client.post("/chat", json={"user_id": "u1", "text": "postgres:16"})
    events = [json.loads(e.removeprefix("data: ")) for e in response.text.split("\n\n") if e and "[DONE]" not in e]
    created = [event for event in events if event.get("action") == "create_file"]
    assert [event["path"] for event in created] == ["postgres.yaml"]
    assert 'tag: "16"' in created[0]["content"]
    assert session.get_session("u1").pending_create is None


def test_any_other_message_drops_the_waiting_create(monkeypatch, fake_llm):
    from hyperion import yamlgen

    async def no_params(text):
        return {}

    monkeypatch.setattr(yamlgen, "llm_params", no_params)
    set_route(monkeypatch, intent="create_file", app_kind="native")
    assert post("create a deployment yaml for my web service") == [prompts.ASK_IMAGE]
    set_route(monkeypatch, intent="question")
    assert text_of(post("What is HyperAI?")).startswith("Hello world")
    assert session.get_session("u1").pending_create is None


def test_chat_rejects_malformed_body():
    assert client.post("/chat", json={"text": "hi"}).status_code == 422
