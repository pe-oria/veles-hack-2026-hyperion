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
        Route(**answer)


def test_parse_route_keeps_conversation_flag_when_a_slot_is_bad():
    route = parse_route({"intent": "question", "about_conversation": True, "app_kind": "cloud"})
    assert route.about_conversation is True


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


def test_guardrail_similarity_thresholds():
    off, question = Route(intent="off_topic"), Route(intent="question")
    assert guardrails.apply(off, "How are edge gadgets registered?", similarity=0.75).intent == "question"
    assert guardrails.apply(off, "Tell me a joke", similarity=0.49).intent == "off_topic"
    assert guardrails.apply(question, "How do I cook lasagna?", similarity=0.5).intent == "off_topic"
    # a far-away question is allowed when it is about the conversation, or names the domain
    chat = Route(intent="question", about_conversation=True)
    assert guardrails.apply(chat, "What did I just ask?", similarity=0.5).intent == "question"
    assert guardrails.apply(question, "Is HyperAI free?", similarity=0.5).intent == "question"
    # retrieval unavailable: trust the router
    assert guardrails.apply(question, "How do I cook lasagna?", similarity=None).intent == "question"


def test_guardrail_follow_up_score_keeps_questions_but_never_rescues_off_topic():
    question, off = Route(intent="question"), Route(intent="off_topic")
    assert guardrails.apply(question, "why?", similarity=0.4, follow_up_similarity=0.7).intent == "question"
    assert guardrails.apply(question, "why?", similarity=0.4, follow_up_similarity=0.5).intent == "off_topic"
    assert guardrails.apply(off, "Tell me a joke", similarity=0.49, follow_up_similarity=0.8).intent == "off_topic"


def test_correct_fills_the_image_slot_the_router_left_empty():
    session = Session()
    created = guardrails.correct(Route(intent="create_file"), "Make a postgres application profile with 2Gi of memory", session)
    assert created.image == "postgres"
    kept = guardrails.correct(Route(intent="create_file", image="redis"), "a redis profile, not postgres", session)
    assert kept.image == "redis"
    # a loose guess is not written into the route, and other intents are left alone
    assert guardrails.correct(Route(intent="create_file"), "a profile for a clickhouse database", session).image is None
    assert guardrails.correct(Route(intent="question"), "what is the nginx image?", session).image is None


def test_correct_applies_the_default_app_kind():
    session = Session()
    assert guardrails.correct(Route(intent="create_file"), "an application profile for a redis container", session).app_kind == "native"
    assert guardrails.correct(Route(intent="create_file"), "firmware manifest for an ESP32-C3 door sensor", session).app_kind == "device"
    assert guardrails.correct(Route(intent="create_file"), "run mosquitto on an edge device", session).app_kind == "device"
    stated = Route(intent="create_file", app_kind="device")
    assert guardrails.correct(stated, "a profile for redis", session).app_kind == "device"
