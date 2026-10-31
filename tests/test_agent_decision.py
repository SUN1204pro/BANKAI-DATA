"""The answer / unanswerable / abstain rule: scoring is +1 / -1 / 0, so answer only when confidence > min_agree."""
import pytest

from datamate.agent import Agent, Cand, Item
from datamate.config import Config
from datamate.llm import MockBackend
from datamate.rules import RuleBook


def ok(v, t="number"):
    return Cand("ok", answer_type=t, answer=v, sql="SELECT 1")


ERR = Cand("error", error="boom")


def un(reason="no data"):
    return Cand("unanswerable", reason=reason)


def decide(cands, **cfg):
    agent = Agent(MockBackend(lambda r: []), Config(backend="mock", **cfg), RuleBook(""))
    t, a, reason, info = agent._decide(Item({"id": "q"}, None, cands=cands))
    return t, a, reason, info


def test_unanimous_answer():
    t, a, _, info = decide([ok(5), ok(5), ok(5.0)])
    assert (t, a, info["confidence"]) == ("number", 5, 1.0)


def test_two_of_three_is_enough_but_one_of_three_is_not():
    assert decide([ok(5), ok(7), ok(5)])[:2] == ("number", 5)
    assert decide([ok(5), ERR, ok(5)])[:2] == ("number", 5)
    assert decide([ok(5), ok(7), ok(9)])[0] == "abstain"
    assert decide([ok(5), ERR, ERR])[0] == "abstain"


def test_a_tie_between_two_samples_abstains_because_expected_score_is_zero():
    assert decide([ok(5), ok(7)])[0] == "abstain"


def test_single_sample_mode_answers():
    assert decide([ok("Cà phê", "string")], n_samples=1)[:2] == ("string", "Cà phê")


def test_threshold_zero_means_plurality_wins():
    assert decide([ok(5), ok(7), ok(9)], min_agree=0.0)[:2] == ("number", 5)  # ties -> the greedy (first) sample


def test_majority_unanswerable_gives_the_reason():
    t, a, reason, _ = decide([un("shop không ghi giá vốn"), un(), ok(5)])
    assert (t, a, reason) == ("unanswerable", None, "shop không ghi giá vốn")


def test_unanswerable_without_a_majority_abstains():
    assert decide([un(), ok(5), ok(7)])[0] == "abstain"
    assert decide([un(), ERR, ok(5)])[0] == "abstain"


def test_nothing_executable_abstains():
    t, a, reason, info = decide([ERR, ERR, ERR])
    assert (t, a, reason) == ("abstain", None, None) and info["why"] == "no executable candidate"


def test_votes_ignore_accent_case_in_strings_and_row_order_in_tables():
    assert decide([ok("Sting dâu", "string"), ok("sting dau", "string"), ok("Pepsi", "string")])[0] == "string"
    t1 = {"columns": ["a", "b"], "rows": [["x", 1], ["y", 2]]}
    t2 = {"columns": ["a", "b"], "rows": [["y", 2], ["x", 1]]}
    assert decide([ok(t1, "table"), ok(t2, "table"), ERR])[0] == "table"


@pytest.mark.parametrize("n", [1, 2, 3])
def test_lists_with_different_order_are_different_answers(n):
    t, *_ = decide([ok(["a", "b"], "list"), ok(["b", "a"], "list"), ok(["a", "b"], "list")][:max(n, 3)])
    assert t == "list"
    assert decide([ok(["a", "b"], "list"), ok(["b", "a"], "list")])[0] == "abstain"
