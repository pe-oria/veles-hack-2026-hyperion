import json

import pytest
from fastapi.testclient import TestClient

import main
from hyperion import actions, confirm, fileops, ide, prompts, router, session
from hyperion.ide import File
from hyperion.router import Route

client = TestClient(main.app)


@pytest.mark.parametrize("text", ["yes", "Yes.", "y", "yeah", "yep", "sure", "ok", "OK!", "confirm", "go ahead",
                                  "do it", "yes please", "Yes, delete it", "yes, go ahead", "sí", "proceed"])
def test_clear_agreement_is_yes(text):
    assert confirm.classify(text) == "yes"


@pytest.mark.parametrize("text", ["no", "No.", "n", "nope", "cancel", "stop", "don't", "do not delete it",
                                  "never mind", "no, keep it", "wait", "abort"])
def test_refusal_is_no(text):
    assert confirm.classify(text) == "no"


@pytest.mark.parametrize("text", ["yes but rename it first", "yes, the other one", "maybe", "which file?", "",
                                  "What is HyperAI?", "yesterday I made a backup", "okay so what does it contain",
                                  "delete the other file instead", "not sure"])
def test_anything_else_is_not_a_yes(text):
    assert confirm.classify(text) != "yes"


@pytest.fixture(autouse=True)
def workspace(monkeypatch) -> dict:
    """One existing file; the router is scripted per message."""
    session._sessions.clear()
    files = {"nginx.yaml": "old"}
    routes = {"Delete nginx.yaml": Route(intent="delete_file", path="nginx.yaml"), "hi": Route(intent="smalltalk")}
    calls = []

    async def read(path):
        if path not in files:
            raise ide.IdeError("not_found", f"I could not find `{path}` in the workspace.")
        return File(path, files[path])

    async def route(text, sess):
        calls.append(text)
        return routes[text]

    monkeypatch.setattr(ide, "read", read)
    monkeypatch.setattr(router, "route", route)
    return {"files": files, "router_calls": calls}


def post(text: str) -> tuple[str, list[dict]]:
    response = client.post("/chat", json={"user_id": "u1", "text": text})
    assert response.text.endswith("data: [DONE]\n\n")
    events = [json.loads(e.removeprefix("data: ")) for e in response.text.split("\n\n") if e and "[DONE]" not in e]
    reply = "".join(event["response"] for event in events if "response" in event)
    return reply, [event for event in events if "action" in event]


def test_yes_executes_the_pending_delete_without_asking_the_router(workspace):
    assert post("Delete nginx.yaml") == ("Delete `nginx.yaml`? (yes/no)", [])
    assert post("yes") == ("Deleted `nginx.yaml`.", [actions.delete_file("nginx.yaml")])
    assert workspace["router_calls"] == ["Delete nginx.yaml"]
    assert session.get_session("u1").pending_action is None


def test_no_cancels(workspace):
    post("Delete nginx.yaml")
    assert post("no") == (prompts.CANCELLED, [])
    assert session.get_session("u1").pending_action is None
    # the confirmation does not linger: a later "yes" deletes nothing
    assert post("yes") == (prompts.NOTHING_PENDING, [])
    assert "nginx.yaml" in workspace["files"]


def test_any_other_reply_drops_the_action_and_is_handled_normally(workspace, monkeypatch):
    post("Delete nginx.yaml")

    async def handle(route, text, sess):
        yield "Created the folder `demo`."

    workspace_route = Route(intent="create_folder", path="demo")

    async def route(text, sess):
        return workspace_route

    monkeypatch.setattr(router, "route", route)
    monkeypatch.setattr(fileops, "handle", handle)
    reply, done = post("Create a folder called demo")
    assert done == []
    assert reply == prompts.NOT_CONFIRMED.format(question="Delete `nginx.yaml`?") + "Created the folder `demo`."
    assert session.get_session("u1").pending_action is None


def test_a_vague_reply_only_reports_that_nothing_was_done(workspace):
    post("Delete nginx.yaml")
    reply, done = post("hi")  # routed as smalltalk: no greeting or refusal is tacked on
    assert done == [] and reply == prompts.NOT_CONFIRMED.format(question="Delete `nginx.yaml`?")
    assert "nginx.yaml" in workspace["files"]


def test_yes_or_no_with_nothing_pending_gets_a_fixed_reply(workspace):
    assert post("yes") == (prompts.NOTHING_PENDING, [])
    assert post("no") == (prompts.NOTHING_PENDING, [])
    assert workspace["router_calls"] == []


@pytest.mark.parametrize("text", ["Delete it", "delete it", "do it", "go ahead", "ok", "sure", "stop", "wait",
                                  "overwrite it", "proceed"])
def test_requests_that_double_as_agreement_reach_the_router_when_nothing_is_pending(workspace, monkeypatch, text):
    seen = []

    async def route(message, sess):
        seen.append(message)
        return Route(intent="smalltalk")

    monkeypatch.setattr(router, "route", route)
    reply, _ = post(text)
    assert seen == [text] and reply != prompts.NOTHING_PENDING


def test_delete_it_twice_asks_then_deletes(workspace, monkeypatch):
    async def route(message, sess):
        return Route(intent="delete_file", path="nginx.yaml")

    monkeypatch.setattr(router, "route", route)
    session.get_session("u1").remember_file("nginx.yaml")
    assert post("Delete it") == ("Delete `nginx.yaml`? (yes/no)", [])
    assert post("delete it") == ("Deleted `nginx.yaml`.", [actions.delete_file("nginx.yaml")])


def test_confirmation_belongs_to_the_user_who_was_asked(workspace):
    post("Delete nginx.yaml")
    other = client.post("/chat", json={"user_id": "someone-else", "text": "hi"})
    assert "action" not in other.text
    assert session.get_session("u1").pending_action is not None
