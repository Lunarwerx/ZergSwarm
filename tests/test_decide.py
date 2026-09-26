"""The decision benchmark's grading: an arm is scored on the option it named, never on how it phrased it."""
from __future__ import annotations

import json

from bench import decide
from zswarm import decisions
from bench.decisions import triage

NOUL = {"id": "n", "type": "noul", "state": "s", "instructions": "q?", "criteria": {"true": "T", "false": "F"}, "gold": True}
CHOICE = {"id": "c", "type": "choice", "state": {"a": 1}, "instructions": "q?", "criteria": {"card_arrival": "card arrival", "card_linking": None}, "gold": "card_linking"}
SCORE = {"id": "s", "type": "score", "state": "s", "instructions": "q?", "criteria": ["low", "mid", "high"], "gold": 2}


def test_options_and_gold_key_per_type():
    assert decisions.options(NOUL) == [("yes", "T"), ("no", "F")] and decide.gold_key(NOUL) == "yes"
    assert [k for k, _ in decisions.options(CHOICE)] == ["card_arrival", "card_linking"] and decide.gold_key(CHOICE) == "card_linking"
    assert [k for k, _ in decisions.options(SCORE)] == ["0", "1", "2"] and decide.gold_key(SCORE) == "2"


def test_parse_final_takes_the_last_final_line():
    keys = ["card_arrival", "card_linking"]
    assert decisions.parse_final("thinking... FINAL: card_arrival\nwait, no.\nFINAL: card_linking", keys) == "card_linking"
    assert decisions.parse_final("FINAL: `Card_Linking`.", keys) == "card_linking"
    assert decisions.parse_final("**FINAL:** card_linking", keys) == "card_linking"


def test_parse_final_maps_true_false_to_yes_no_and_refuses_ambiguity():
    assert decisions.parse_final("FINAL: True", ["yes", "no"]) == "yes"
    assert decisions.parse_final("FINAL: no", ["yes", "no"]) == "no"
    assert decisions.parse_final("FINAL: A or B", ["A", "B", "C", "D"]) is None
    assert decisions.parse_final("", ["yes", "no"]) is None


def test_parse_final_does_not_match_a_key_inside_another_word():
    # "0" must not match the 0 in "10", nor "no" the no in "not_mentioned"
    assert decisions.parse_final("FINAL: 10", ["0", "1", "2"]) is None
    assert decisions.parse_final("FINAL: not_mentioned", ["supported", "contradicted", "not_mentioned"]) == "not_mentioned"


def test_render_fences_multiline_options_and_shows_structured_state():
    item = {"type": "choice", "state": {"docstring": "d"}, "instructions": "Which?", "criteria": {"A": "def f():\n    return 1", "B": "x"}}
    text = decisions.render(item)
    assert "```\ndef f():\n    return 1\n```" in text and "- B: x" in text and '"docstring": "d"' in text


def test_jev_question_omits_absent_criteria():
    assert "criteria" not in decisions.jev_question({**NOUL, "criteria": None})
    assert decisions.jev_question(SCORE)["criteria"] == ["low", "mid", "high"]


def test_to_row_grades_and_keeps_jev_probability_of_gold():
    row = decide.to_row("r", "triage", "v", "jev-latest", "jev-latest", None, NOUL, 0,
                        {"status": "ok", "pred": "no", "probs": {"yes": 0.3, "no": 0.7}, "conf": 0.4, "parsed": True, "cost": 1e-6, "s": 0.1})
    assert (row["pass"], row["gold"], row["p_gold"], row["suite"]) == (False, "yes", 0.3, "decisions.triage")
    err = decide.to_row("r", "triage", "v", "m", "m", None, NOUL, 0, {"status": "error", "error": "HTTP 429"})
    assert err["pass"] is None and err["status"] == "error"  # a limit is never a wrong answer


def test_parse_arm():
    assert decide.parse_arm("jev") == ("jev-latest", "jev-latest", None, True)
    assert decide.parse_arm("jev-1.13.0")[3] is True
    assert decide.parse_arm("jev#b20") == ("jev-latest#b20", "jev-latest", None, True) and decide.batch_size("jev-latest#b20") == 20
    assert decide.batch_size("jev-latest") == 1
    assert decide.parse_arm("groq-gpt-oss-120b@low") == ("groq-gpt-oss-120b@low", "groq-gpt-oss-120b", "low", False)


def test_triage_items_are_well_formed():
    ids = [it["id"] for it in triage.ITEMS]
    assert len(ids) == len(set(ids))
    for it in triage.ITEMS:
        keys = [k for k, _ in decisions.options(it)]
        assert decide.gold_key(it) in keys, it["id"]
        json.dumps(it)


def test_the_committed_suites_load_and_every_gold_is_an_option():
    for s in decide.all_suites():
        items = decide.load_suite(s)
        assert items, s
        for it in items:
            assert decide.gold_key(it) in [k for k, _ in decisions.options(it)], it["id"]
