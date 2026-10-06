"""Measure the routing decision (router + guardrail) against the live models.

    uv run python scripts/eval_router.py                    # every case
    uv run python scripts/eval_router.py --split test
    uv run python scripts/eval_router.py --category off_topic --concurrency 1

Cases live in tests/eval/router_cases.yaml. A markdown summary is written to
docs/eval/router_<split>_<YYYYmmdd-HHMM>.md.
"""

import argparse
import asyncio
import logging
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
from hyperion.session import Session  # noqa: E402

try:  # the multi-step planner arrives with Task B
    from hyperion import planner  # noqa: E402
except ImportError:
    planner = None

FILE_IN_REPLY = re.compile(r"[\w./-]+\.ya?ml")
CASES_PATH = ROOT / "tests" / "eval" / "router_cases.yaml"
REPORT_DIR = ROOT / "docs" / "eval"


@dataclass
class Result:
    case: dict
    ok: bool
    got: str
    seconds: float
    error: str = ""


def load_cases(split: str = "all", category: str | None = None) -> list[dict]:
    cases = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
    return [
        case
        for case in cases
        if (split == "all" or case["split"] == split) and (category is None or case["category"] == category)
    ]


def slot_matches(got: object, expected: object) -> bool:
    """Booleans must be equal; strings match as a case-insensitive prefix."""
    if isinstance(expected, bool):
        return got is expected
    return str(got or "").lower().startswith(str(expected).lower())


def check(expect: dict, routes: list) -> bool:
    if "intents" in expect:
        return [route.intent for route in routes] == expect["intents"]
    if len(routes) != 1:
        return False
    return all(slot_matches(getattr(routes[0], key), value) for key, value in expect.items())


def describe(routes: list) -> str:
    if len(routes) != 1:
        return "[" + ", ".join(route.intent for route in routes) + "]"
    shown = routes[0].model_dump(exclude_defaults=True)
    return ", ".join(f"{key}={value}" for key, value in {"intent": routes[0].intent, **shown}.items())


async def predict(case: dict) -> list:
    """The routes the service would act on for this message, exactly as `main.run_turn` decides."""
    session = Session()
    for user, assistant in case.get("history") or []:
        session.add_turn(user, assistant)
        # the live service would remember the files those turns touched
        for path in FILE_IN_REPLY.findall(assistant):
            session.remember_file(path)
    text = case["text"]
    steps = [text]
    if planner is not None and planner.looks_multi(text):
        steps = await planner.split(text, session)
    return [(await main.classify(step, session))[0] for step in steps]


async def run_case(case: dict, gate: asyncio.Semaphore) -> Result:
    async with gate:
        started = time.perf_counter()
        try:
            routes = await predict(case)
        except Exception as exc:  # one broken call must not lose the whole run
            return Result(case, False, "", time.perf_counter() - started, f"{type(exc).__name__}: {exc}")
        return Result(case, check(case["expect"], routes), describe(routes), time.perf_counter() - started)


def expected_text(expect: dict) -> str:
    if "intents" in expect:
        return "[" + ", ".join(expect["intents"]) + "]"
    return ", ".join(f"{key}={value}" for key, value in expect.items())


def summarise(results: list[Result], split: str, category: str | None, concurrency: int, wall: float) -> str:
    by_category: dict[str, list[Result]] = defaultdict(list)
    for result in results:
        by_category[result.case["category"]].append(result)

    def rate(group: list[Result]) -> str:
        passed = sum(result.ok for result in group)
        return f"{passed}/{len(group)} ({passed / len(group):.0%})" if group else "-"

    single = [result for result in results if result.case["category"] != "multi_step"]
    lines = [
        f"# Router evaluation - split `{split}`" + (f", category `{category}`" if category else ""),
        "",
        f"- Date: {datetime.now():%Y-%m-%d %H:%M}",
        f"- Model: {main.llm.MODEL} (router + guardrail, the decision `main.run_turn` acts on)",
        f"- Planner for multi-step requests: {'yes' if planner else 'no (Task B not implemented)'}",
        f"- Cases: {len(results)}, concurrency {concurrency}, wall time {wall:.0f}s",
        f"- **Accuracy: {rate(results)}**; without `multi_step`: {rate(single)}",
        f"- Avg latency per case: {sum(r.seconds for r in results) / len(results):.2f}s",
        "",
        "| Category | dev | test | all |",
        "|---|---|---|---|",
    ]
    for name in sorted(by_category):
        group = by_category[name]
        dev = [result for result in group if result.case["split"] == "dev"]
        test = [result for result in group if result.case["split"] == "test"]
        lines.append(f"| {name} | {rate(dev)} | {rate(test)} | {rate(group)} |")
    dev = [result for result in results if result.case["split"] == "dev"]
    test = [result for result in results if result.case["split"] == "test"]
    lines.append(f"| **total** | {rate(dev)} | {rate(test)} | {rate(results)} |")

    failures = [result for result in results if not result.ok]
    lines += ["", f"## Failures ({len(failures)})", ""]
    if failures:
        lines += ["| id | split | message | expected | got |", "|---|---|---|---|---|"]
        for result in failures:
            case = result.case
            got = result.error or result.got
            text = case["text"].replace("|", "\\|")
            lines.append(f"| {case['id']} | {case['split']} | {text} | {expected_text(case['expect'])} | {got} |")
    else:
        lines.append("None.")
    return "\n".join(lines) + "\n"


async def run(split: str, category: str | None, concurrency: int) -> tuple[list[Result], float]:
    cases = load_cases(split, category)
    if not cases:
        raise SystemExit("no cases match the given --split / --category")
    gate = asyncio.Semaphore(concurrency)
    started = time.perf_counter()
    results = await asyncio.gather(*(run_case(case, gate) for case in cases))
    return list(results), time.perf_counter() - started


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", choices=["dev", "test", "all"], default="all")
    parser.add_argument("--category")
    parser.add_argument("--concurrency", type=int, default=2, help="parallel cases (the GPU is shared)")
    parser.add_argument("--no-report", action="store_true", help="print only, do not write docs/eval/")
    args = parser.parse_args()

    # a filter, not a level: importing the app configures logging again
    for noisy in ("httpx", "hyperion"):
        logging.getLogger(noisy).addFilter(lambda record: record.levelno >= logging.WARNING)
    results, wall = asyncio.run(run(args.split, args.category, args.concurrency))
    report = summarise(results, args.split, args.category, args.concurrency, wall)
    print(report)
    if not args.no_report:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        name = f"router_{args.split}" + (f"_{args.category}" if args.category else "")
        path = REPORT_DIR / f"{name}_{datetime.now():%Y%m%d-%H%M}.md"
        path.write_text(report, encoding="utf-8")
        print(f"report written to {path.relative_to(ROOT)}")


if __name__ == "__main__":
    cli()
