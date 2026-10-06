"""Drive the running service through multi-turn scenarios and score what it does.

    uv run python scripts/eval_e2e.py                       # every scenario
    uv run python scripts/eval_e2e.py --category multi_step
    uv run python scripts/eval_e2e.py --scenario lifecycle-nginx

Needs the agent on :8000 (AGENT_URL) and the ide-backend on :3001 (IDE_BACKEND_URL). The script
plays the IDE: it applies every action to the backend the moment it arrives, as the browser does.

Scenarios live in tests/eval/e2e_scenarios.yaml. A markdown summary is written to
docs/eval/e2e_<YYYYmmdd-HHMM>.md.

Reported numbers:
  action correctness   turns whose emitted actions equal the expected ones
  first-pass valid     YAML written by create/edit that is valid as first emitted
  valid after repair   YAML that is valid when the turn ends (after the agent's own fixes)
"""

import argparse
import json
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ide_sim import AGENT, BACKEND, apply, resolve  # noqa: E402

SCENARIOS_PATH = ROOT / "tests" / "eval" / "e2e_scenarios.yaml"
REPORT_DIR = ROOT / "docs" / "eval"
SCRATCH = "_eval_scratch"  # copies of first-pass YAML are validated here, away from the agent's file
REFUSAL_MARK = "I can only help with HyperAI"
WRITES = ("create_file", "edit_file")


@dataclass
class TurnResult:
    scenario: str
    category: str
    text: str
    reply: str
    actions: list[dict]
    seconds: float
    first_event_seconds: float | None
    done: bool
    problems: list[str] = field(default_factory=list)
    actions_ok: bool = True
    first_pass_valid: bool | None = None  # None: the turn wrote no YAML
    final_valid: bool | None = None

    @property
    def ok(self) -> bool:
        return not self.problems


def is_yaml(path: str) -> bool:
    return path.lower().endswith((".yaml", ".yml"))


def validate(client: httpx.Client, path: str) -> dict:
    response = client.get(f"{BACKEND}/agent/validation/file", params={"path": path})
    return response.json() if response.status_code == 200 else {"valid": False, "errors": [{"message": response.text}]}


def validate_content(client: httpx.Client, content: str) -> bool:
    """Validate a copy, so the verdict on the first write cannot race the agent's repair."""
    path = f"{SCRATCH}/{uuid.uuid4().hex}.yaml"
    client.post(f"{BACKEND}/file", json={"path": path, "content": content})
    try:
        return bool(validate(client, path).get("valid"))
    finally:
        client.delete(f"{BACKEND}/delete", params={"path": path})


def chat(client: httpx.Client, user_id: str, text: str) -> tuple[str, list[dict], float, float | None, bool]:
    """One turn: (reply text, actions, total seconds, seconds to first event, saw [DONE])."""
    reply, actions, first, done = "", [], None, False
    started = time.perf_counter()
    with client.stream("POST", f"{AGENT}/chat", json={"user_id": user_id, "text": text}) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.startswith("data: "):
                continue
            if first is None:
                first = time.perf_counter() - started
            data = line[len("data: "):]
            if data == "[DONE]":
                done = True
                break
            event = json.loads(data)
            if "action" in event:
                event["_applied"] = apply(client, event)
                actions.append(event)
            else:
                reply += event.get("response", "")
    return reply, actions, time.perf_counter() - started, first, done


def action_matches(expected: str, action: dict) -> bool:
    name, _, path = expected.partition(" ")
    return action["action"] == name and (not path or action["path"] == path)


def score_turn(client: httpx.Client, scenario: dict, turn: dict, user_id: str) -> TurnResult:
    expect = turn.get("expect") or {}
    reply, actions, seconds, first, done = chat(client, user_id, turn["text"])
    result = TurnResult(scenario["id"], scenario["category"], turn["text"], reply, actions, seconds, first, done)
    if not done:
        result.problems.append("stream ended without [DONE]")

    emitted = [f"{action['action']} {action['path']}" for action in actions]
    if "actions" in expect:
        wanted = expect["actions"]
        if len(wanted) != len(actions) or not all(action_matches(w, a) for w, a in zip(wanted, actions)):
            result.actions_ok = False
            result.problems.append(f"actions: expected {wanted}, got {emitted}")
    for action in actions:
        if action["_applied"] != "ok":
            result.actions_ok = False
            result.problems.append(f"IDE could not apply {action['action']} {action['path']}: {action['_applied']}")

    writes = [action for action in actions if action["action"] in WRITES and is_yaml(action["path"])]
    if writes:
        first_by_path: dict[str, str] = {}
        for action in writes:
            first_by_path.setdefault(action["path"], action["content"])
        result.first_pass_valid = all(validate_content(client, content) for content in first_by_path.values())
        reports = {path: validate(client, resolve(client, path)) for path in first_by_path}
        result.final_valid = all(report.get("valid") for report in reports.values())
        if expect.get("valid") and not result.final_valid:
            errors = [
                f"{path}: {error.get('field', '')} {error.get('message', '')}".strip()
                for path, report in reports.items()
                for error in report.get("errors", [])[:3]
            ]
            result.problems.append("YAML not valid after the turn: " + "; ".join(errors))
    elif expect.get("valid"):
        result.problems.append("expected a valid YAML file, but the turn wrote none")

    lowered = reply.lower()
    for needle in expect.get("contains", []):
        if needle.lower() not in lowered:
            result.problems.append(f"reply lacks {needle!r}")
    if expect.get("refused") and REFUSAL_MARK.lower() not in lowered:
        result.problems.append("expected the off-topic refusal")
    return result


def created_paths(results: list[TurnResult]) -> list[str]:
    paths = []
    for result in results:
        for action in result.actions:
            if action["action"] in ("create_file", "create_folder", "edit_file"):
                paths.append(action["path"])
    return paths


def cleanup(client: httpx.Client, paths: list[str]) -> None:
    # top-level entries only: deleting "demo" removes "demo/nginx.yaml" with it
    for top in sorted({path.split("/", 1)[0] for path in paths}):
        client.delete(f"{BACKEND}/delete", params={"path": top})


def run_scenario(client: httpx.Client, scenario: dict) -> list[TurnResult]:
    user_id = f"eval-{scenario['id']}-{uuid.uuid4().hex[:8]}"
    setup_files = (scenario.get("setup") or {}).get("files") or {}
    expected_paths = [
        expected.partition(" ")[2]
        for turn in scenario["turns"]
        for expected in (turn.get("expect") or {}).get("actions", [])
        if " " in expected
    ]
    cleanup(client, [*setup_files, *expected_paths])  # leftovers of an aborted run must not skew this one
    for path, content in setup_files.items():
        client.post(f"{BACKEND}/file", json={"path": path, "content": content})
    results: list[TurnResult] = []
    try:
        for turn in scenario["turns"]:
            try:
                results.append(score_turn(client, scenario, turn, user_id))
            except httpx.HTTPError as exc:
                failed = TurnResult(scenario["id"], scenario["category"], turn["text"], "", [], 0.0, None, False)
                failed.problems.append(f"request failed: {exc}")
                failed.actions_ok = False
                results.append(failed)
    finally:
        cleanup(client, [*setup_files, *expected_paths, *created_paths(results), SCRATCH])
    return results


def percent(part: int, whole: int) -> str:
    return f"{part}/{whole} ({part / whole:.0%})" if whole else "n/a"


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def summarise(results: list[TurnResult], scenarios: list[dict], agent_info: str) -> str:
    wrote = [result for result in results if result.first_pass_valid is not None]
    seconds = [result.seconds for result in results if result.done]
    firsts = [result.first_event_seconds for result in results if result.first_event_seconds is not None]
    by_scenario: dict[str, list[TurnResult]] = {}
    for result in results:
        by_scenario.setdefault(result.scenario, []).append(result)
    passed = [name for name, turns in by_scenario.items() if all(turn.ok for turn in turns)]

    lines = [
        "# End-to-end evaluation",
        "",
        f"- Date: {datetime.now():%Y-%m-%d %H:%M}",
        f"- Agent: {AGENT} ({agent_info}); IDE backend: {BACKEND}",
        f"- Scenarios: {len(by_scenario)}, turns: {len(results)}",
        "",
        "| Metric | Result |",
        "|---|---|",
        f"| Scenarios fully passed | {percent(len(passed), len(by_scenario))} |",
        f"| Turns fully passed (actions, reply and validity) | {percent(sum(r.ok for r in results), len(results))} |",
        f"| **Action correctness** (turns with the expected actions) | {percent(sum(r.actions_ok for r in results), len(results))} |",
        f"| **YAML first-pass valid** | {percent(sum(bool(r.first_pass_valid) for r in wrote), len(wrote))} |",
        f"| **YAML valid after repair** | {percent(sum(bool(r.final_valid) for r in wrote), len(wrote))} |",
        f"| Latency per turn, p50 / p95 | {percentile(seconds, 0.5):.2f}s / {percentile(seconds, 0.95):.2f}s |",
        f"| Time to first streamed event, p50 / p95 | {percentile(firsts, 0.5):.2f}s / {percentile(firsts, 0.95):.2f}s |",
        f"| Mean latency per turn | {statistics.mean(seconds):.2f}s |",
        "",
        "| Category | Scenarios passed | Turns passed | Action correctness |",
        "|---|---|---|---|",
    ]
    categories = sorted({scenario["category"] for scenario in scenarios})
    for category in categories:
        turns = [result for result in results if result.category == category]
        names = {result.scenario for result in turns}
        good = [name for name in names if name in passed]
        lines.append(
            f"| {category} | {percent(len(good), len(names))} | {percent(sum(t.ok for t in turns), len(turns))} "
            f"| {percent(sum(t.actions_ok for t in turns), len(turns))} |"
        )

    lines += ["", "## Scenarios", "", "| Scenario | Category | Turns passed | Result |", "|---|---|---|---|"]
    for name, turns in by_scenario.items():
        good = sum(turn.ok for turn in turns)
        lines.append(f"| {name} | {turns[0].category} | {good}/{len(turns)} | {'pass' if name in passed else 'FAIL'} |")

    failures = [result for result in results if not result.ok]
    lines += ["", f"## Failed turns ({len(failures)})", ""]
    for result in failures:
        reply = " ".join(result.reply.split())
        lines += [
            f"- **{result.scenario}** - \"{result.text}\"",
            *[f"  - {problem}" for problem in result.problems],
            f"  - reply: {reply[:240]}{'...' if len(reply) > 240 else ''}",
        ]
    if not failures:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--category")
    parser.add_argument("--scenario")
    parser.add_argument("--agent-info", default="", help="label for the report, e.g. the image tag under test")
    parser.add_argument("--no-report", action="store_true", help="print only, do not write docs/eval/")
    args = parser.parse_args()

    scenarios = yaml.safe_load(SCENARIOS_PATH.read_text(encoding="utf-8"))
    scenarios = [
        scenario
        for scenario in scenarios
        if (not args.category or scenario["category"] == args.category)
        and (not args.scenario or scenario["id"] == args.scenario)
    ]
    if not scenarios:
        raise SystemExit("no scenario matches the given --category / --scenario")

    results: list[TurnResult] = []
    with httpx.Client(timeout=180) as client:
        try:
            client.get(f"{AGENT}/health").raise_for_status()
            client.get(f"{BACKEND}/files").raise_for_status()
        except httpx.HTTPError as exc:
            raise SystemExit(f"the agent ({AGENT}) and the ide-backend ({BACKEND}) must both be running: {exc}")
        for scenario in scenarios:
            turns = run_scenario(client, scenario)
            results += turns
            status = "pass" if all(turn.ok for turn in turns) else "FAIL"
            print(f"{status}  {scenario['id']}  ({sum(t.ok for t in turns)}/{len(turns)} turns)", flush=True)

    report = summarise(results, scenarios, args.agent_info or "unlabelled")
    print("\n" + report)
    if not args.no_report:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORT_DIR / f"e2e_{datetime.now():%Y%m%d-%H%M}.md"
        path.write_text(report, encoding="utf-8")
        print(f"report written to {path.relative_to(ROOT)}")


if __name__ == "__main__":
    cli()
