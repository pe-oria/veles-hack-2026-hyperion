"""Talk to the agent from a terminal and apply its actions to the IDE backend, like the IDE does.

    uv run python scripts/ide_sim.py "Create a deployment YAML for nginx" ["next message" ...]
    HYPERION_USER=alice uv run python scripts/ide_sim.py "Delete it"

Needs the agent on :8000 and the ide-backend on :3001.
"""

import json
import os
import sys

import httpx

AGENT = os.environ.get("AGENT_URL", "http://localhost:8000")
BACKEND = os.environ.get("IDE_BACKEND_URL", "http://localhost:3001/api")
USER = os.environ.get("HYPERION_USER", "ide-sim")


def resolve(client: httpx.Client, path: str) -> str:
    """Bare names are looked up across the workspace, as the IDE does for edit/delete."""
    found = client.get(f"{BACKEND}/agent/file", params={"path": path})
    return found.json().get("path", path) if found.status_code == 200 else path


def apply(client: httpx.Client, event: dict) -> str:
    action, path = event["action"], event["path"]
    if action == "create_folder":
        result = client.post(f"{BACKEND}/folder/create", json={"path": path})
    elif action == "create_file":
        result = client.post(f"{BACKEND}/file/create", json={"path": path, "content": event["content"]})
    elif action == "edit_file":
        result = client.post(f"{BACKEND}/file", json={"path": resolve(client, path), "content": event["content"]})
    elif action == "delete_file":
        result = client.delete(f"{BACKEND}/delete", params={"path": resolve(client, path)})
    elif action == "delete_folder":
        result = client.delete(f"{BACKEND}/delete", params={"path": path})
    else:
        return f"unknown action {action}"
    return "ok" if result.status_code == 200 else f"FAILED {result.status_code} {result.text}"


def chat(client: httpx.Client, text: str) -> None:
    print(f"\n> {text}")
    with client.stream("POST", f"{AGENT}/chat", json={"user_id": USER, "text": text}) as response:
        for line in response.iter_lines():
            if not line.startswith("data: "):
                continue
            data = line[len("data: "):]
            if data == "[DONE]":
                print("\n[DONE]")
                return
            event = json.loads(data)
            if "action" in event:
                # applied at once: the agent is waiting for the file before it validates it
                print(f"\n  [{event['action']} {event['path']} -> {apply(client, event)}]", end="", flush=True)
            else:
                print(event["response"], end="", flush=True)
    print("\n(stream closed without [DONE])")


if __name__ == "__main__":
    with httpx.Client(timeout=120) as http:
        for message in sys.argv[1:]:
            chat(http, message)
