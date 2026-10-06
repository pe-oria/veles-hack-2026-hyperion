from hyperion import guardrails, prompts
from hyperion.router import Route, build_messages, parse_route
from hyperion.session import MAX_TURNS, Session


def test_parse_route_full():
    route = parse_route({"intent": "create_file", "path": "a.yaml", "app_kind": "native", "image": "nginx"})
    assert (route.intent, route.path, route.app_kind, route.image) == ("create_file", "a.yaml", "native", "nginx")


def test_parse_route_keeps_intent_when_a_slot_is_bad():
    route = parse_route({"intent": "delete_file", "app_kind": "kubernetes", "path": 7})
    assert route.intent == "delete_file"
    assert route.app_kind is None


def test_parse_route_falls_back_to_question():
    assert parse_route({}).intent == "question"
    assert parse_route({"intent": "dance"}).intent == "question"


def test_parse_route_blank_slots_become_none():
    route = parse_route({"intent": "edit_file", "path": "null", "image": " "})
    assert route.path is None and route.image is None


def test_router_prompt_contains_history_and_latest_message():
    session = Session()
    session.add_turn("Create nginx yaml", "Created nginx.yaml")
    last = build_messages("Delete it", session)[-1][1]
    assert "Created nginx.yaml" in last and last.endswith("Latest message: Delete it")


def test_router_examples_are_valid_routes():
    for _, _, answer in prompts.ROUTER_EXAMPLES:
        Route.model_validate_json(answer)


def test_session_keeps_only_recent_turns():
    session = Session()
    for i in range(MAX_TURNS + 3):
        session.add_turn(f"q{i}", f"a{i}")
    assert len(session.history) == 2 * MAX_TURNS
    assert session.history[0] == ("user", "q3")


def test_guardrail_overrides_off_topic_for_domain_terms():
    off = Route(intent="off_topic")
    assert guardrails.apply(off, "What is a DeviceNode?").intent == "question"
    assert guardrails.apply(off, "What is the weather today?").intent == "off_topic"
    assert guardrails.apply(Route(intent="smalltalk"), "hi hyperion").intent == "smalltalk"
