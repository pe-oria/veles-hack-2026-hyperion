import json

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

import main
from hyperion import ide, llm, planner, prompts, router, session, yamlgen
from hyperion.ide import File, IdeError
from hyperion.router import Route
from hyperion.session import Session

client = TestClient(main.app)


# --- the gate --------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "Create a folder demo and put an nginx deployment YAML in it",
        "make yamls for redis and postgres",
        "Create nginx.yaml and then validate it",
        "change the memory to 2Gi and then show me the file",
        "create a folder called apps, then delete the folder old",
        "What is a DeviceNode? Also create a device app for the hello-world image",
        "generate profiles for nginx, redis and mongo",
        "validate it and delete it afterwards",
        "tell me a joke and create an nginx yaml",
        "delete nginx.yaml and then create a redis yaml",
        "create a profile for kafka and mysql",
        "1. create a folder logs\n2. create a redis yaml in it",
        "create two folders, staging and live",
        "create a folder svc and a deployment yaml for my web service in it",
        "first remove nginx.yaml, then make a fresh one for nginx:1.27",
        "I want a kafka profile and a mysql profile",
        "delete a.yaml and remove b.yaml",
        "I'd like a directory called infra with a rabbitmq descriptor in it, 1Gi of RAM",
        "now do the same for memcached and influxdb",
        "do the same for busybox",
        "lower rabbitmq's memory to 512Mi, then tell me whether the file still validates",
        "wipe the infra directory, and afterwards explain what an Open Connector is",
        "could you whip up manifests for minio and keycloak, and write a limerick while you're at it",
        "is the prometheus one valid? and what does D4.3 say about the Application Profile Manager",
        "get me a descriptor for an esp32c3 sensor and another for an android apk at https://ex.org/a.apk",
        "remove the stack folder and the esp32 file",
        "Is caddy.yaml fine? If so raise its storage to 5Gi",
        "Explain what a DeviceNode is and then sing happy birthday",
        "likewise for nats",
        "Who maintains HyperAI, and by the way purge the lab folder",
    ],
)
def test_looks_multi_accepts_requests_for_several_things(text):
    assert planner.looks_multi(text)


@pytest.mark.parametrize(
    "text",
    [
        "Create an nginx deployment with cpu 500m and memory 1Gi",  # one file, two settings
        "Create a deployment YAML for a service using the nginx Docker image",
        "Change the memory to 2Gi",
        "set the cpu to 2 cores and the image tag to 7.2",
        "write an app profile for myuser/app:1.2 listening on port 9000 and save it as services/app.yaml",
        "Generate an application profile for a redis container and save it to demo/redis.yaml",
        "I need a device app descriptor for our Android camera app com.acme.cam, the apk is at https://acme.io/cam.apk",
        "How do I deploy my first web server?",
        "What is HyperAI and what are Open Connectors?",
        "and how are they registered?",
        "What's the difference between a native app and a device app?",
        "What is a DeviceNode?",
        "Which fields are required in a native application profile?",
        "Delete it",
        "yes",
        "remove everything in demo",
        "Hello!",
        "What is the weather today?",
        "expose port 8080 as well",
        "show me nginx.yaml and print its contents",  # two ways of saying "read it"
        "set the cpu to 500m and change the memory to 1Gi",
        "I need a descriptor for our camera app, save it as apps/cam.yaml",
        "I do not need old.yaml anymore, delete it",
        "change the chip to esp32c6 and make it production",
        "write a yaml for a redis cache and put it under the cache directory",
        "create a redis profile and save it in the demo folder",
        "double its cpu, expose 8443 too and switch it to the testing phase",  # three settings, one file
        "What is HyperAI like?",
        "I'd like a redis profile with 1Gi of memory",
        "I'd like to deploy our monitoring app to a phone, write the manifest",
    ],
)
def test_looks_multi_leaves_single_requests_alone(text):
    assert not planner.looks_multi(text)


# --- splitting -------------------------------------------------------------------------------

@pytest.fixture
def planner_reply(monkeypatch) -> dict:
    state = {"reply": {}}

    async def ask_json(messages, max_tokens=None):
        state["prompt"] = messages[-1][1]
        return state["reply"]

    monkeypatch.setattr(llm, "ask_json", ask_json)
    return state


async def test_split_returns_the_steps_and_shows_the_model_the_context(planner_reply):
    planner_reply["reply"] = {"steps": ["Create a folder called demo", "Create an nginx yaml in the demo folder"]}
    known = Session()
    known.remember_file("web.yaml")
    steps = await planner.split("Create a folder demo and put an nginx yaml in it", known)
    assert steps == ["Create a folder called demo", "Create an nginx yaml in the demo folder"]
    assert planner_reply["prompt"].startswith("Last file: web.yaml. Last folder: none.")


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"steps": "create a folder"},
        {"steps": ["only one thing"]},
        {"steps": ["same", "Same "]},
        {"steps": [1, 2]},
        {"apiVersion": "apps/v1"},
    ],
)
async def test_split_falls_back_to_the_original_message(planner_reply, reply):
    planner_reply["reply"] = reply
    assert await planner.split("create a folder and a file", Session()) == ["create a folder and a file"]


async def test_split_may_return_one_rewritten_step_only_for_a_request_that_refers_back(planner_reply):
    previous = Session()
    previous.add_turn("Create a device app for the hello-world image", "Creating hello-world.yaml.")
    planner_reply["reply"] = {"steps": ["Create a device app for the busybox image"]}
    assert await planner.split("do the same for busybox", previous) == ["Create a device app for the busybox image"]
    assert "Previous request: Create a device app for the hello-world image" in planner_reply["prompt"]
    # without such a reference a single step means "leave my message alone"
    assert await planner.split("create a folder and nothing else", previous) == ["create a folder and nothing else"]


async def test_the_previous_request_is_hidden_unless_the_message_refers_to_it(planner_reply):
    """Shown unconditionally, the model folds it into the plan and repeats it."""
    previous = Session()
    previous.add_turn("delete nginx.yaml", "Deleted nginx.yaml.")
    planner_reply["reply"] = {"steps": ["Delete nginx.yaml", "Create a folder called logs", "Create a folder called tmp"]}
    text = "create a folder called logs and a folder called tmp"
    await planner.split(text, previous)
    assert "Previous request: none" in planner_reply["prompt"] and "delete nginx.yaml" not in planner_reply["prompt"]
    # and a plan that restates it anyway is refused: nginx.yaml is not something this message names
    assert await planner.split(text, previous) == [text]


async def test_details_of_the_previous_request_may_be_repeated(planner_reply):
    previous = Session()
    previous.add_turn("Create a kafka descriptor with 2Gi of memory in the logs folder", "Creating logs/kafka.yaml.")
    planner_reply["reply"] = {"steps": ["Create a mysql descriptor with 2Gi of memory in the logs folder",
                                        "Create a mariadb descriptor with 2Gi of memory in the logs folder"]}
    assert len(await planner.split("same again for mysql and mariadb", previous)) == 2


async def test_split_caps_the_number_of_steps(planner_reply):
    planner_reply["reply"] = {"steps": [f"create folder {name}" for name in "abcdefgh"]}
    assert len(await planner.split("create folders a b c d e f g h", Session())) == planner.MAX_STEPS


async def test_split_rejects_details_the_user_never_gave(planner_reply):
    text = "make yamls for redis and postgres"
    planner_reply["reply"] = {"steps": ["Create a yaml for redis:7 on port 6379", "Create a yaml for postgres"]}
    assert await planner.split(text, Session()) == [text]
    planner_reply["reply"] = {"steps": ["Create secrets.yaml for redis", "Create a yaml for postgres"]}
    assert await planner.split(text, Session()) == [text]
    # in a later step an invented file name is neutralised rather than trusted
    planner_reply["reply"] = {"steps": ["Create a yaml for redis", "Create secrets.yaml for postgres"]}
    assert await planner.split(text, Session()) == ["Create a yaml for redis", "Create the file for postgres"]
    # a lifecycle phase the model made up is simply removed: the step is fine without it
    planner_reply["reply"] = {"steps": ["Create a production yaml for redis", "Create a yaml for postgres"]}
    assert await planner.split(text, Session()) == ["Create a yaml for redis", "Create a yaml for postgres"]
    stated = "make production yamls for redis and postgres"
    planner_reply["reply"] = {"steps": ["Create a production yaml for redis", "Create a production yaml for postgres"]}
    assert await planner.split(stated, Session()) == planner_reply["reply"]["steps"]
    planner_reply["reply"] = {"steps": ["Create a yaml for redis", "Create a yaml for mysql"]}
    assert await planner.split(text, Session()) == [text]
    # a file the session knows may be named even if this message only said "it"
    known = Session()
    known.remember_file("demo/web.yaml")
    planner_reply["reply"] = {"steps": ["Validate web.yaml", "Delete web.yaml"]}
    assert await planner.split("validate it and delete it", known) == ["Validate web.yaml", "Delete the file"]


async def test_later_steps_do_not_inherit_the_file_from_before_the_message(planner_reply):
    """"the file" in step 2 is the one step 1 works on, not the session's previous file."""
    known = Session()
    known.remember_file("infra/rabbitmq.yaml")
    known.remember_file("infra/influxdb.yaml")
    planner_reply["reply"] = {"steps": ["Lower the memory of rabbitmq to 512Mi", "Check if infra/influxdb.yaml still validates"]}
    steps = await planner.split("lower rabbitmq's memory to 512Mi, then tell me whether the file still validates", known)
    assert steps == ["Lower the memory of rabbitmq to 512Mi", "Check if the file still validates"]
    # a name the user wrote is kept
    planner_reply["reply"] = {"steps": ["Create nginx.yaml for nginx", "Show me nginx.yaml"]}
    assert await planner.split("Create nginx.yaml for nginx and then show me the file", Session()) == [
        "Create nginx.yaml for nginx", "Show me nginx.yaml"]


# --- running a plan --------------------------------------------------------------------------

@pytest.fixture
def world(monkeypatch) -> dict:
    """A scripted planner and router over an in-memory workspace that applies actions at once."""
    session._sessions.clear()
    state = {"files": {}, "plans": {}, "routes": {}, "router_calls": []}

    async def split(text, sess):
        return state["plans"].get(text, [text])

    async def route(text, sess):
        state["router_calls"].append(text)
        found = state["routes"][text]
        if isinstance(found, Exception):
            raise found
        return found

    async def read(path):
        matches = [p for p in state["files"] if p == path or ("/" not in path and p.rsplit("/", 1)[-1] == path)]
        if not matches:
            raise IdeError("not_found", f"I could not find {path} in the workspace.")
        return File(matches[0], state["files"][matches[0]])

    async def find(path):
        try:
            return await read(path)
        except IdeError:
            return None

    async def saved(path, content, timeout=None):
        state["files"][path] = content  # the IDE writes the file while the reply is still streaming
        return True

    async def valid(path):
        return {"path": path, "type": "native", "valid": True, "errors": [], "warnings": []}

    async def no_params(text):
        return {}

    monkeypatch.setattr(planner, "split", split)
    monkeypatch.setattr(router, "route", route)
    monkeypatch.setattr(ide, "read", read)
    monkeypatch.setattr(ide, "find", find)
    monkeypatch.setattr(ide, "wait_for_content", saved)
    monkeypatch.setattr(ide, "validate", valid)
    monkeypatch.setattr(yamlgen, "llm_params", no_params)
    return state


def post(world: dict, text: str) -> tuple[str, list[str]]:
    """One turn; applies the actions to the fake workspace and returns (reply, ["action path", ...])."""
    response = client.post("/chat", json={"user_id": "u1", "text": text})
    assert response.text.endswith("data: [DONE]\n\n")
    events = [json.loads(e.removeprefix("data: ")) for e in response.text.split("\n\n") if e and "[DONE]" not in e]
    done = []
    for event in events:
        if "action" not in event:
            continue
        done.append(f"{event['action']} {event['path']}")
        if event["action"] in ("create_file", "edit_file"):
            world["files"][event["path"]] = event["content"]
        elif event["action"] == "delete_file":
            world["files"].pop(event["path"], None)
    return "".join(event.get("response", "") for event in events), done


def test_two_creates_run_in_order_with_a_plan_and_step_headers(world):
    text = "make yamls for redis and postgres"
    world["plans"][text] = ["Create a yaml for redis", "Create a yaml for postgres"]
    world["routes"] = {step: Route(intent="create_file") for step in world["plans"][text]}

    reply, done = post(world, text)
    assert done == ["create_file redis.yaml", "create_file postgres.yaml"]
    assert reply.startswith("I'll do 2 things:\n1) Create a yaml for redis\n2) Create a yaml for postgres")
    assert "Step 1/2 - Create a yaml for redis\nCreating redis.yaml" in reply
    assert "Step 2/2 - Create a yaml for postgres\nCreating postgres.yaml" in reply
    assert 'uri: "postgres"' in world["files"]["postgres.yaml"]


def test_folder_then_file_puts_the_file_inside_the_folder(world):
    text = "Create a folder demo and put an nginx deployment YAML in it"
    world["plans"][text] = ["Create a folder called demo", "Create a deployment YAML for nginx in the demo folder"]
    world["routes"] = {
        "Create a folder called demo": Route(intent="create_folder", path="demo"),
        "Create a deployment YAML for nginx in the demo folder": Route(intent="create_file", path="demo"),
    }
    _, done = post(world, text)
    assert done == ["create_folder demo", "create_file demo/nginx.yaml"]
    assert session.get_session("u1").last_file == "demo/nginx.yaml"


def test_later_steps_see_what_earlier_steps_did(world):
    text = "Create nginx.yaml for nginx and then show me the file"
    world["plans"][text] = ["Create nginx.yaml for nginx", "Show me nginx.yaml"]
    world["routes"] = {
        "Create nginx.yaml for nginx": Route(intent="create_file", path="nginx.yaml"),
        "Show me nginx.yaml": Route(intent="read_file", path="nginx.yaml"),
    }
    reply, done = post(world, text)
    assert done == ["create_file nginx.yaml"] and "nginx.yaml:\n\napplicationProfile:" in reply


def destructive_plan(world: dict) -> str:
    world["files"]["nginx.yaml"] = "old"
    text = "create a folder logs, delete nginx.yaml and then create a redis yaml"
    world["plans"][text] = ["Create a folder called logs", "Delete nginx.yaml", "Create a redis yaml"]
    world["routes"] = {
        "Create a folder called logs": Route(intent="create_folder", path="logs"),
        "Delete nginx.yaml": Route(intent="delete_file", path="nginx.yaml"),
        "Create a redis yaml": Route(intent="create_file"),
    }
    return text


def test_a_destructive_step_pauses_the_plan_and_yes_resumes_it(world):
    reply, done = post(world, destructive_plan(world))
    assert done == ["create_folder logs"] and "nginx.yaml" in world["files"]
    assert reply.endswith("Delete nginx.yaml? (yes/no)\n\nStill to do once you answer:\n3) Create a redis yaml")
    assert session.get_session("u1").pending_steps == [(3, "Create a redis yaml")]

    reply, done = post(world, "yes")
    assert done == ["delete_file nginx.yaml", "create_file redis.yaml"]
    assert "Deleted nginx.yaml." in reply and "Step 3/3 - Create a redis yaml" in reply
    assert session.get_session("u1").pending_steps == []


def test_no_cancels_the_action_and_drops_the_queue(world):
    post(world, destructive_plan(world))
    reply, done = post(world, "no")
    assert done == [] and "nginx.yaml" in world["files"]
    assert reply == prompts.CANCELLED + "\n\nNot done:\n3) Create a redis yaml"
    assert session.get_session("u1").pending_steps == []
    assert post(world, "yes") == (prompts.NOTHING_PENDING, [])


def test_any_other_reply_drops_the_queue_and_is_answered_normally(world):
    post(world, destructive_plan(world))
    world["routes"]["create a folder called other"] = Route(intent="create_folder", path="other")
    reply, done = post(world, "create a folder called other")
    assert done == ["create_folder other"] and "nginx.yaml" in world["files"]
    assert reply.startswith("I did not get a yes, so I left things as they were: Delete nginx.yaml?")
    assert "Not done:\n3) Create a redis yaml\n\nCreated the folder other." in reply
    assert session.get_session("u1").pending_steps == []


def test_a_failing_step_is_reported_and_the_rest_still_runs(world):
    text = "create folders a, b and c"
    world["plans"][text] = ["Create a folder called a", "Create a folder called b", "Create a folder called c"]
    world["routes"] = {
        "Create a folder called a": Route(intent="create_folder", path="a"),
        "Create a folder called b": RuntimeError("model timed out"),
        "Create a folder called c": Route(intent="create_folder", path="c"),
    }
    reply, done = post(world, text)
    assert done == ["create_folder a", "create_folder c"]
    assert f"Step 2/3 - Create a folder called b\n{prompts.PLAN_STEP_FAILED}" in reply


def test_a_rejected_key_stops_the_plan_with_the_configuration_error(world):
    text = "create folders a and b"
    world["plans"][text] = ["Create a folder called a", "Create a folder called b"]
    response = httpx.Response(401, request=httpx.Request("POST", "https://legion1.di.uoa.gr/v1/chat/completions"))
    rejected = openai.AuthenticationError("bad key", response=response, body=None)
    world["routes"] = {"Create a folder called a": rejected, "Create a folder called b": rejected}
    reply, done = post(world, text)
    assert done == [] and reply.endswith(prompts.REJECTED_KEY)
    assert world["router_calls"] == ["Create a folder called a"]


def test_an_off_topic_step_is_refused_while_the_others_run(world):
    text = "tell me a joke and create an nginx yaml"
    world["plans"][text] = ["tell me a joke", "Create an nginx yaml"]
    world["routes"] = {"tell me a joke": Route(intent="off_topic"), "Create an nginx yaml": Route(intent="create_file")}

    async def no_hits(queries, k=4):
        return [[] for _ in queries]

    main.rag.search, original = no_hits, main.rag.search
    try:
        reply, done = post(world, text)
    finally:
        main.rag.search = original
    assert done == ["create_file nginx.yaml"]
    assert f"Step 1/2 - tell me a joke\n{prompts.REFUSAL}" in reply


def test_a_step_that_needs_an_image_pauses_and_the_answer_resumes_the_plan(world):
    text = "create a folder svc and a deployment yaml for my web service in it"
    world["plans"][text] = ["Create a folder called svc", "Create a deployment yaml for my web service in the svc folder",
                            "Create a folder called done"]
    world["routes"] = {
        "Create a folder called svc": Route(intent="create_folder", path="svc"),
        "Create a deployment yaml for my web service in the svc folder": Route(intent="create_file", path="svc"),
        "Create a folder called done": Route(intent="create_folder", path="done"),
    }
    reply, done = post(world, text)
    assert done == ["create_folder svc"] and prompts.ASK_IMAGE in reply and "3) Create a folder called done" in reply

    reply, done = post(world, "traefik")
    assert done == ["create_file svc/traefik.yaml", "create_folder done"]


def test_a_request_that_refers_back_runs_as_its_rewritten_form(world):
    world["plans"]["do the same for busybox"] = ["Create a device app for the busybox image"]
    world["routes"]["Create a device app for the busybox image"] = Route(intent="create_file", app_kind="device")
    reply, done = post(world, "do the same for busybox")
    assert done == ["create_file busybox.yaml"] and not reply.startswith("I'll do")
    assert world["router_calls"] == ["Create a device app for the busybox image"]


def test_a_rewritten_request_that_is_itself_several_things_is_split_again(world):
    world["plans"]["likewise for nats"] = ["Create a lab folder holding a nats profile with 250m cpu"]
    world["plans"]["Create a lab folder holding a nats profile with 250m cpu"] = [
        "Create a folder called lab", "Create a nats profile with 250m cpu in the lab folder"]
    world["routes"] = {
        "Create a folder called lab": Route(intent="create_folder", path="lab"),
        "Create a nats profile with 250m cpu in the lab folder": Route(intent="create_file"),
    }
    reply, done = post(world, "likewise for nats")
    assert done == ["create_folder lab", "create_file lab/nats.yaml"]
    assert 'cpu: "250m"' in world["files"]["lab/nats.yaml"]


def test_a_compound_question_the_planner_leaves_whole_is_answered_once(world, monkeypatch):
    text = "What is HyperAI, and then how do I deploy an app?"
    assert planner.looks_multi(text)  # the gate lets it through; the planner returns it as one step
    world["routes"][text] = Route(intent="smalltalk")
    assert post(world, text) == (prompts.SMALLTALK, [])
    assert world["router_calls"] == [text]


def test_single_requests_never_reach_the_planner(world, monkeypatch):
    async def must_not_split(text, sess):
        raise AssertionError("the planner must not be called for a single request")

    monkeypatch.setattr(planner, "split", must_not_split)
    world["routes"]["Create a folder called demo"] = Route(intent="create_folder", path="demo")
    assert post(world, "Create a folder called demo") == ("Created the folder demo.", ["create_folder demo"])


def test_a_gate_hit_that_is_one_request_runs_normally(world):
    text = "create a folder called logs, and nothing else please"  # passes the gate? only if it looks multi
    world["routes"][text] = Route(intent="create_folder", path="logs")
    reply, done = post(world, text)
    assert done == ["create_folder logs"] and not reply.startswith("I'll do")
