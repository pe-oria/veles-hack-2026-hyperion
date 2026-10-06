import pytest

from hyperion import actions, fileops, ide, yamlgen
from hyperion.actions import PathError
from hyperion.ide import File, IdeError
from hyperion.router import Route
from hyperion.session import Session
from hyperion.yamlgen import clean_params, render

NGINX = render(clean_params({}, "native", "nginx"))


class FakeIde:
    """An in-memory workspace that applies actions instantly, like a very fast IDE."""

    def __init__(self, monkeypatch) -> None:
        self.files: dict[str, str] = {}
        self.reports: list[dict] = []  # queued validation reports; valid when empty
        self.applies = True
        monkeypatch.setattr(ide, "read", self.read)
        monkeypatch.setattr(ide, "find", self.find)
        monkeypatch.setattr(ide, "wait_for_content", self.wait_for_content)
        monkeypatch.setattr(ide, "validate", self.validate)

    async def read(self, path: str) -> File:
        matches = [p for p in self.files if p == path or ("/" not in path and p.rsplit("/", 1)[-1] == path)]
        if not matches:
            raise IdeError("not_found", f"I could not find {path} in the workspace.")
        if len(matches) > 1:
            raise IdeError("ambiguous", f"Several files are named {path}.", matches)
        return File(matches[0], self.files[matches[0]])

    async def find(self, path: str) -> File | None:
        try:
            return await self.read(path)
        except IdeError as exc:
            if exc.kind == "not_found":
                return None
            raise

    async def wait_for_content(self, path: str, content: str, timeout=None) -> bool:
        return self.applies

    async def validate(self, path: str) -> dict:
        report = self.reports.pop(0) if self.reports else {"valid": True, "errors": [], "warnings": []}
        return {"path": path, "type": "native", **report}

    def apply(self, event: dict) -> None:
        if event["action"] in ("create_file", "edit_file"):
            self.files[event["path"]] = event["content"]
        elif event["action"] == "delete_file":
            del self.files[event["path"]]


@pytest.fixture
def workspace(monkeypatch) -> FakeIde:
    return FakeIde(monkeypatch)


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch) -> dict:
    """Stand-ins for the three LLM passes; tests override the entries they care about."""
    state = {"params": {}, "edit": lambda content, text: content, "repair": lambda content, errors: content}

    async def extract_params(text, kind, image_hint):
        return clean_params(state["params"], kind, image_hint)

    async def edit(content, instruction):
        return state["edit"](content, instruction)

    async def repair(content, errors):
        return state["repair"](content, errors)

    monkeypatch.setattr(yamlgen, "extract_params", extract_params)
    monkeypatch.setattr(yamlgen, "edit", edit)
    monkeypatch.setattr(yamlgen, "repair", repair)
    return state


async def run(workspace: FakeIde, session: Session, text: str, **route) -> tuple[str, list[dict]]:
    """Run one file intent; returns (reply text, actions) and applies the actions."""
    reply, done = "", []
    async for event in fileops.handle(Route(**route), text, session):
        if isinstance(event, dict):
            workspace.apply(event)
            done.append(event)
        else:
            reply += event
    return reply, done


async def test_create_file_renders_a_valid_profile_and_validates_it(workspace):
    session = Session()
    reply, done = await run(
        workspace, session, "Create a deployment YAML for nginx", intent="create_file", app_kind="native", image="nginx"
    )
    assert done == [actions.create_file("nginx.yaml", NGINX)]
    assert reply == (
        "Creating nginx.yaml - a native application profile for nginx:latest.\n\nValidating with the IDE... passed."
    )
    assert session.last_file == "nginx.yaml" and session.files == ["nginx.yaml"]


async def test_create_file_in_a_named_folder_and_with_an_explicit_path(workspace):
    session = Session()
    _, done = await run(workspace, session, "put an nginx profile in the demo folder", intent="create_file",
                        path="demo", image="nginx")
    assert done[0]["path"] == "demo/nginx.yaml"
    _, done = await run(workspace, session, "nginx profile as web/site.yaml", intent="create_file",
                        path="web/site.yaml", image="nginx")
    assert done[0]["path"] == "web/site.yaml"


async def test_same_name_in_another_folder_does_not_block_creation(workspace):
    workspace.files["demo/nginx.yaml"] = "other"
    _, done = await run(workspace, Session(), "nginx yaml please", intent="create_file", image="nginx")
    assert [event["path"] for event in done] == ["nginx.yaml"]


async def test_created_file_is_not_validated_when_the_ide_never_saves_it(workspace):
    workspace.applies = False
    reply, done = await run(workspace, Session(), "nginx yaml please", intent="create_file", image="nginx")
    assert len(done) == 1 and reply.endswith("skipped: I could not confirm that the IDE saved the file.")


async def test_edit_uses_the_last_file_and_shows_a_diff(workspace, fake_llm):
    session = Session()
    await run(workspace, session, "nginx yaml please", intent="create_file", image="nginx")
    fake_llm["edit"] = lambda content, text: content.replace('memory: "1Gi"', 'memory: "2Gi"')

    reply, done = await run(workspace, session, "Change the memory to 2Gi", intent="edit_file")
    assert [(event["action"], event["path"]) for event in done] == [("edit_file", "nginx.yaml")]
    assert reply == (
        'Working on nginx.yaml... done:\n  - memory: "1Gi"\n  + memory: "2Gi"\n\nValidating with the IDE... passed.'
    )
    assert 'memory: "2Gi"' in workspace.files["nginx.yaml"]


async def test_edit_that_breaks_the_yaml_or_changes_nothing_leaves_the_file_alone(workspace, fake_llm):
    workspace.files["nginx.yaml"] = NGINX
    reply, done = await run(workspace, Session(), "make nginx.yaml better", intent="edit_file", path="nginx.yaml")
    assert done == [] and reply == "Working on nginx.yaml... I could not work out what to change. Can you rephrase the change?"

    fake_llm["edit"] = lambda content, text: "key: [unclosed"
    reply, done = await run(workspace, Session(), "make nginx.yaml better", intent="edit_file", path="nginx.yaml")
    assert done == [] and "left the file unchanged" in reply


async def test_validator_errors_after_an_edit_are_repaired(workspace, fake_llm):
    workspace.files["nginx.yaml"] = NGINX
    bad = {"valid": False, "errors": [{"line": 20, "field": "specs.resources.cpu", "message": "must be string"}]}
    workspace.reports = [bad]
    fake_llm["edit"] = lambda content, text: content.replace('cpu: "1000m"', "cpu: 2")
    fake_llm["repair"] = lambda content, errors: content.replace("cpu: 2", 'cpu: "2000m"')

    reply, done = await run(workspace, Session(), "set cpu to 2 in nginx.yaml", intent="edit_file", path="nginx.yaml")
    assert [event["action"] for event in done] == ["edit_file", "edit_file"]
    assert reply.endswith("Validating with the IDE... found 1 problem(s), fixing them.\n\nValidating again... passed.")
    assert 'cpu: "2000m"' in workspace.files["nginx.yaml"]


async def test_repair_gives_up_after_two_rounds_and_reports_the_errors(workspace, fake_llm):
    workspace.files["nginx.yaml"] = NGINX
    bad = {"valid": False, "errors": [{"line": 3, "field": "metadata.owner", "message": "is required"}]}
    workspace.reports = [bad, bad, bad]
    counter = iter(range(100))
    fake_llm["edit"] = lambda content, text: content + "# edited\n"
    fake_llm["repair"] = lambda content, errors: content + f"# try {next(counter)}\n"

    reply, done = await run(workspace, Session(), "tweak nginx.yaml", intent="edit_file", path="nginx.yaml")
    assert len(done) == 1 + fileops.MAX_REPAIRS
    assert "Validating again... failed." in reply
    assert "has 1 error(s)" in reply and "metadata.owner is required" in reply


async def test_fix_request_is_driven_by_the_validation_report(workspace, fake_llm):
    workspace.files["broken.yaml"] = NGINX.replace('owner: "my-team"\n', "")
    errors = [{"line": 2, "field": "applicationProfile.metadata.owner", "message": "is required"}]
    workspace.reports = [{"valid": False, "errors": errors}]
    seen = {}

    def repair(content, reported):
        seen["errors"] = reported
        return NGINX

    fake_llm["repair"] = repair
    reply, done = await run(workspace, Session(), "Fix broken.yaml", intent="edit_file", path="broken.yaml")
    assert seen["errors"] == errors and len(done) == 1 and reply.endswith("Validating with the IDE... passed.")

    reply, done = await run(workspace, Session(), "Fix broken.yaml", intent="edit_file", path="broken.yaml")
    assert done == [] and "nothing to fix" in reply


async def confirm_pending(workspace: FakeIde, session: Session) -> tuple[str, list[dict]]:
    """Answer "yes" to the pending question."""
    pending, session.pending_action = session.pending_action, None
    reply, done = "", []
    async for event in fileops.execute_pending(pending, session):
        if isinstance(event, dict):
            workspace.apply(event)
            done.append(event)
        else:
            reply += event
    return reply, done


async def test_delete_asks_first_and_only_acts_once_confirmed(workspace):
    workspace.files["demo/nginx.yaml"] = NGINX
    session = Session()
    session.remember_file("demo/nginx.yaml")

    reply, done = await run(workspace, session, "Delete it", intent="delete_file")
    assert done == [] and reply == "Delete demo/nginx.yaml? (yes/no)"
    assert "demo/nginx.yaml" in workspace.files and session.last_file == "demo/nginx.yaml"
    assert session.pending_action.action == actions.delete_file("demo/nginx.yaml")

    reply, done = await confirm_pending(workspace, session)
    assert done == [actions.delete_file("demo/nginx.yaml")] and reply == "Deleted demo/nginx.yaml."
    assert workspace.files == {} and session.last_file is None and session.files == []


async def test_overwrite_asks_first_then_replaces_with_edit_file_and_validates(workspace):
    workspace.files["nginx.yaml"] = "old content"
    session = Session()
    reply, done = await run(workspace, session, "nginx yaml please", intent="create_file", image="nginx")
    assert done == [] and reply.startswith("nginx.yaml already exists. Overwrite it") and reply.endswith("(yes/no)")
    assert workspace.files["nginx.yaml"] == "old content"

    reply, done = await confirm_pending(workspace, session)
    assert done == [actions.edit_file("nginx.yaml", NGINX)]  # create_file would fail on an existing file
    assert reply.startswith("Overwrote nginx.yaml") and reply.endswith("Validating with the IDE... passed.")
    assert workspace.files["nginx.yaml"] == NGINX and session.last_file == "nginx.yaml"


async def test_missing_ambiguous_and_unnamed_files_become_messages_not_actions(workspace):
    reply, done = await run(workspace, Session(), "delete missing.yaml", intent="delete_file", path="missing.yaml")
    assert done == [] and "could not find missing.yaml" in reply

    workspace.files.update({"a/app.yaml": "x", "b/app.yaml": "y"})
    reply, done = await run(workspace, Session(), "delete app.yaml", intent="delete_file", path="app.yaml")
    assert done == [] and "Several files are named" in reply

    session = Session()
    reply, done = await run(workspace, session, "Delete it", intent="delete_file")
    assert done == [] and "Which file do you mean" in reply
    assert session.pending_action is None  # nothing to confirm when there is nothing to delete


@pytest.mark.parametrize("path", ["../../etc/passwd.yaml", "/etc/hosts", "~/x.yaml", "C:\\\\x.yaml", "a/../../b.yaml"])
async def test_paths_outside_the_workspace_are_refused(workspace, path):
    for intent in ("create_file", "delete_file", "create_folder", "delete_folder", "edit_file"):
        reply, done = await run(workspace, Session(), f"do it to {path}", intent=intent, path=path)
        assert done == [], intent
        assert "not allowed" in reply or "absolute path" in reply or "not a valid" in reply


async def test_invented_paths_are_ignored(workspace):
    session = Session()
    # the router made up a location from earlier turns: the user named neither folder nor file
    _, done = await run(workspace, session, "write a descriptor for an ESP32 sensor", intent="create_file",
                        path="apps/esp32-s3-temp.yaml", app_kind="device")
    assert done[0]["path"] == "my-app.yaml"

    workspace.files["keep.yaml"] = "x"
    reply, done = await run(workspace, Session(), "Delete it", intent="delete_file", path="keep.yaml")
    assert done == [] and "Which file do you mean" in reply


async def test_folders(workspace):
    session = Session()
    reply, done = await run(workspace, session, "Create a folder called demo", intent="create_folder", path="demo")
    assert done == [actions.create_folder("demo")] and session.last_folder == "demo"
    session.remember_file("demo/nginx.yaml")

    reply, done = await run(workspace, session, "delete that folder", intent="delete_folder")
    assert done == [] and session.folders == ["demo"]
    assert reply == "Delete the folder demo and everything in it? It contains demo/nginx.yaml. (yes/no)"

    reply, done = await confirm_pending(workspace, session)
    assert done == [actions.delete_folder("demo")] and reply == "Deleted the folder demo."
    assert session.folders == [] and session.files == []

    reply, done = await run(workspace, Session(), "delete that folder", intent="delete_folder")
    assert done == [] and "Which folder" in reply


async def test_validate_and_read(workspace):
    workspace.files["nginx.yaml"] = NGINX
    session = Session()
    workspace.reports = [{"valid": False, "errors": [{"line": 4, "field": "a.b", "message": "is required"}],
                          "warnings": [{"line": 9, "field": "a.c", "message": "unknown field"}]}]
    reply, _ = await run(workspace, session, "Validate nginx.yaml", intent="validate_file", path="nginx.yaml")
    assert "has 1 error(s)" in reply and "- line 4: a.b is required" in reply and "1 warning(s)" in reply
    assert session.last_file == "nginx.yaml"

    reply, _ = await run(workspace, session, "show it", intent="read_file")
    assert reply.startswith("nginx.yaml:\n\napplicationProfile:")


def test_clean_path():
    assert actions.clean_path(" ./demo//nginx.yaml ") == "demo/nginx.yaml"
    assert actions.clean_path("`demo\\\\sub`") == "demo/sub"
    for bad in ("", "..", "a/../b", "/abs", "x;rm -rf.yaml"):
        with pytest.raises(PathError):
            actions.clean_path(bad)
