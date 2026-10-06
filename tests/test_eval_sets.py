"""The evaluation sets are data we rely on: keep them well-formed and keep `test` held out."""

import sys
from collections import Counter
from pathlib import Path

import yaml

from hyperion import prompts
from hyperion.router import Route

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import eval_router  # noqa: E402

CASES = yaml.safe_load((ROOT / "tests" / "eval" / "router_cases.yaml").read_text(encoding="utf-8"))
SCENARIOS = yaml.safe_load((ROOT / "tests" / "eval" / "e2e_scenarios.yaml").read_text(encoding="utf-8"))
INTENTS = set(Route.model_fields["intent"].annotation.__args__)
ACTIONS = {"create_folder", "delete_folder", "create_file", "edit_file", "delete_file"}


def test_router_cases_are_well_formed():
    assert len(CASES) >= 100
    assert len({case["id"] for case in CASES}) == len(CASES)
    for case in CASES:
        assert case["split"] in ("dev", "test"), case["id"]
        assert case["text"].strip(), case["id"]
        expect = case["expect"]
        intents = expect["intents"] if "intents" in expect else [expect["intent"]]
        assert set(intents) <= INTENTS, case["id"]
        assert all(len(pair) == 2 for pair in case.get("history") or []), case["id"]
        if "intents" in expect:
            assert len(intents) >= 2 and case["category"] == "multi_step", case["id"]


def test_router_cases_cover_every_category_in_both_splits():
    counts = Counter((case["category"], case["split"]) for case in CASES)
    for category in {case["category"] for case in CASES}:
        assert counts[(category, "dev")] >= 2 and counts[(category, "test")] >= 2, category
    dev_share = sum(case["split"] == "dev" for case in CASES) / len(CASES)
    assert 0.5 <= dev_share <= 0.7


def test_held_out_cases_are_not_few_shot_examples():
    examples = {text.strip().lower() for _, text, _ in prompts.ROUTER_EXAMPLES}
    examples |= {text.strip().lower() for text, _ in prompts.PARAMS_EXAMPLES}
    leaked = [case["id"] for case in CASES if case["split"] == "test" and case["text"].strip().lower() in examples]
    assert leaked == []


def test_check_rules():
    nginx = Route(intent="create_file", image="nginx:1.27", app_kind="native", path="Demo/nginx.yaml")
    assert eval_router.check({"intent": "create_file", "image": "nginx", "path": "demo"}, [nginx])
    assert not eval_router.check({"intent": "create_file", "image": "redis"}, [nginx])
    assert not eval_router.check({"intent": "edit_file"}, [nginx])
    assert eval_router.check({"intent": "question", "about_conversation": False}, [Route(intent="question")])
    assert not eval_router.check({"intent": "question", "about_conversation": True}, [Route(intent="question")])
    two = [Route(intent="create_folder"), Route(intent="create_file")]
    assert eval_router.check({"intents": ["create_folder", "create_file"]}, two)
    assert not eval_router.check({"intents": ["create_folder", "create_file"]}, two[:1])
    assert not eval_router.check({"intent": "create_folder"}, two)


def test_e2e_scenarios_are_well_formed():
    assert len({scenario["id"] for scenario in SCENARIOS}) == len(SCENARIOS)
    for scenario in SCENARIOS:
        assert scenario["turns"], scenario["id"]
        for turn in scenario["turns"]:
            expect = turn.get("expect") or {}
            assert set(expect) <= {"actions", "contains", "refused", "valid"}, scenario["id"]
            for action in expect.get("actions", []):
                assert action.split(" ")[0] in ACTIONS, scenario["id"]
