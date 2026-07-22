"""Tests for stage 1 triage.

No LLM is called here. What these pin down is the part of stage 1 that is not a
prompt: the evidence rule. The design's position is that telling a model to
require a quote is not a control — verifying the quote is. These tests are that
verification.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grill.llm import (
    Completion,
    LLMError,
    extract_json_array,
    extract_json_object,
    salvage_flat_object,
)
from grill.transcript import Session, Turn
from grill.triage import (
    MOMENT_KEYS,
    build_prompt,
    parse_moments,
    render_session,
    triage,
)


def session(*texts: str, files: set[str] | None = None) -> Session:
    return Session(
        session_id="0198e4f1",
        path=Path("/tmp/0198e4f1.jsonl"),
        git_branch="main",
        turns=[Turn(text=t, timestamp=None, index=i) for i, t in enumerate(texts)],
        files_touched=files or set(),
    )


def completion(text: str) -> Completion:
    return Completion(text=text, cost_usd=0.01, duration_ms=1200)


class TestRenderSession:
    def test_includes_every_developer_turn(self):
        rendered = render_session(session("why an idempotency key?", "ok ship it"))
        assert "why an idempotency key?" in rendered
        assert "ok ship it" in rendered

    def test_lists_file_paths_but_not_contents(self):
        rendered = render_session(session("go", files={"/tmp/app/payments.py"}))
        assert "/tmp/app/payments.py" in rendered
        assert "files edited (1)" in rendered

    def test_truncates_a_giant_pasted_turn(self):
        rendered = render_session(session("x" * 9000))
        assert "chars truncated" in rendered
        assert len(rendered) < 9000

    def test_prompt_survives_braces_in_developer_text(self):
        # The prompt template uses str.format; a JSON blob pasted by the
        # developer must not be interpreted as a format field.
        prompt = build_prompt(session('i ran {"verdict": "ask"} and it broke'))
        assert '{"verdict": "ask"}' in prompt


def moments_json(*moments: dict) -> str:
    import json

    return json.dumps(list(moments))


def moment(**overrides) -> dict:
    base = {
        "turn": 0,
        "signal": "asked_why",
        "topic": "idempotency keys",
        "quote": "why an idempotency key?",
        "shows": "the developer asking why this key is needed",
    }
    base.update(overrides)
    return base


class TestParseMoments:
    def test_keeps_a_moment_whose_quote_is_in_the_named_turn(self):
        found, rejected = parse_moments(
            session("why an idempotency key?"), completion(moments_json(moment()))
        )
        assert [m.topic for m in found] == ["idempotency keys"]
        assert found[0].turn == 0
        assert rejected == []

    def test_keeps_every_qualifying_moment(self):
        text = moments_json(
            moment(turn=0),
            moment(turn=1, signal="pushed_back", quote="no, use a ledger", topic="ledgers"),
        )
        found, _ = parse_moments(
            session("why an idempotency key?", "no, use a ledger"), completion(text)
        )
        assert len(found) == 2

    def test_rejects_a_fabricated_quote(self):
        found, rejected = parse_moments(
            session("ship it"), completion(moments_json(moment()))
        )
        assert found == []
        assert "not found" in rejected[0]

    def test_rejects_a_quote_that_is_in_a_different_turn(self):
        # Anchoring to the named turn is stricter than the old whole-session
        # search, and it is what makes the turn index trustworthy as identity.
        found, rejected = parse_moments(
            session("ship it", "why an idempotency key?"),
            completion(moments_json(moment(turn=0))),
        )
        assert found == []
        assert "not found" in rejected[0]

    def test_rejects_a_turn_index_the_session_does_not_have(self):
        found, rejected = parse_moments(
            session("why an idempotency key?"), completion(moments_json(moment(turn=42)))
        )
        assert found == []
        assert "no turn 42" in rejected[0]

    def test_rejects_asked_why_when_the_quote_asks_nothing(self):
        found, rejected = parse_moments(
            session("we should use a ledger"),
            completion(moments_json(moment(quote="we should use a ledger"))),
        )
        assert found == []
        assert "asks nothing" in rejected[0]

    def test_rejects_an_unrecognized_signal(self):
        found, rejected = parse_moments(
            session("why an idempotency key?"),
            completion(moments_json(moment(signal="vibes"))),
        )
        assert found == []
        assert "unrecognized signal" in rejected[0]

    def test_marks_code_grounded_signals_weak(self):
        found, _ = parse_moments(
            session("why an idempotency key?"),
            completion(moments_json(moment(signal="new_pattern"))),
        )
        assert found[0].weak_evidence is True

    def test_quote_provable_signals_are_not_weak(self):
        found, _ = parse_moments(
            session("why an idempotency key?"), completion(moments_json(moment()))
        )
        assert found[0].weak_evidence is False

    def test_one_bad_moment_does_not_discard_the_good_ones(self):
        text = moments_json(moment(turn=0), moment(turn=1, quote="never typed this"))
        found, rejected = parse_moments(
            session("why an idempotency key?", "ok"), completion(text)
        )
        assert len(found) == 1
        assert len(rejected) == 1

    def test_an_empty_array_is_silence_not_an_error(self):
        found, rejected = parse_moments(session("ship it"), completion("[]"))
        assert found == []
        assert rejected == []

    def test_a_salvaged_moment_still_faces_the_evidence_rule(self):
        # Array-side analog of the deleted test_salvaged_ask_still_faces_the_
        # evidence_rule: salvage recovers a moment's text, it does not exempt
        # that text from the quote-in-turn check.
        text = (
            '[{"turn": "0", "signal": "asked_why", "topic": "retries", '
            '"quote": "why does the "retry" wrapper not double-charge?", '
            '"shows": "asks why"}]'
        )
        found, rejected = parse_moments(session("ship it"), completion(text))
        assert found == []
        assert "not found" in rejected[0]

    def test_a_salvaged_moments_string_turn_is_coerced_and_matched(self):
        quote = 'why does the "retry" wrapper not double-charge?'
        text = (
            '[{"turn": "1", "signal": "asked_why", "topic": "retries", '
            f'"quote": "{quote}", "shows": "asks why"}}]'
        )
        found, rejected = parse_moments(session("ship it", quote), completion(text))
        assert rejected == []
        assert found[0].turn == 1
        assert found[0].quote == quote

    def test_a_salvaged_moment_with_an_unquoted_turn_is_kept_when_the_quote_is_genuine(self):
        # The case the coordinator flagged: element-recovery only earns its
        # complexity if a moment recovered with a bare-integer `turn` can
        # still pass the evidence rule and be kept.
        quote = 'why does the "retry" wrapper not double-charge?'
        text = (
            '[{"turn": 1, "signal": "asked_why", "topic": "retries", '
            f'"quote": "{quote}", "shows": "asks why"}}]'
        )
        found, rejected = parse_moments(session("ship it", quote), completion(text))
        assert rejected == []
        assert len(found) == 1
        assert found[0].turn == 1
        assert found[0].quote == quote

    def test_missing_turn_produces_a_sensible_rejection_message(self):
        text = moments_json(
            {"signal": "asked_why", "topic": "t", "quote": "x", "shows": "s"}
        )
        found, rejected = parse_moments(session("x"), completion(text))
        assert found == []
        assert "no turn None" not in rejected[0]


class TestTriageVerdictFromMoments:
    def test_selected_moment_populates_the_verdict(self, monkeypatch):
        text = moments_json(
            moment(turn=0, signal="pushed_back", quote="no, use a ledger", topic="ledgers"),
            moment(turn=1, quote="why an idempotency key?"),
        )
        monkeypatch.setattr(
            "grill.triage.complete", lambda prompt, **kw: completion(text)
        )
        verdict = triage(session("no, use a ledger", "why an idempotency key?"))
        assert verdict.kept
        # asked_why outranks pushed_back regardless of position in the response.
        assert verdict.signal == "asked_why"
        assert verdict.topic == "idempotency keys"
        assert verdict.candidates == 2
        assert len(verdict.moments) == 2

    def test_no_qualifying_moments_is_silence(self, monkeypatch):
        monkeypatch.setattr("grill.triage.complete", lambda prompt, **kw: completion("[]"))
        verdict = triage(session("ship it"))
        assert not verdict.kept
        assert verdict.candidates == 0

    def test_all_moments_rejected_records_the_demotion(self, monkeypatch):
        text = moments_json(moment(quote="never typed this"))
        monkeypatch.setattr("grill.triage.complete", lambda prompt, **kw: completion(text))
        verdict = triage(session("ship it"))
        assert not verdict.kept
        assert verdict.demoted_from_ask is True
        assert "not found" in verdict.reason

    def test_an_llm_failure_is_silence_not_a_crash(self, monkeypatch):
        def boom(prompt, **kw):
            raise LLMError("claude timed out after 180s")

        monkeypatch.setattr("grill.triage.complete", boom)
        verdict = triage(session("why an idempotency key?"))
        assert not verdict.kept
        assert verdict.error == "claude timed out after 180s"

    def test_one_unescaped_quote_still_yields_a_real_verdict_from_survivors(self, monkeypatch):
        text = (
            '[{"turn": 0, "signal": "asked_why", "topic": "idempotency keys", '
            '"quote": "why an idempotency key?", "shows": "asks why"}, '
            '{"turn": "1", "signal": "pushed_back", "topic": "retries", '
            '"quote": "no, use the "retry" wrapper instead", '
            '"shows": "corrects the approach"}]'
        )
        monkeypatch.setattr("grill.triage.complete", lambda prompt, **kw: completion(text))
        verdict = triage(
            session("why an idempotency key?", 'no, use the "retry" wrapper instead')
        )
        assert verdict.kept
        assert verdict.candidates == 2
        assert len(verdict.moments) == 2
        # asked_why outranks pushed_back, so the surviving turn-0 moment wins.
        assert verdict.signal == "asked_why"
        assert verdict.topic == "idempotency keys"

    def test_transport_failure_and_parse_failure_report_different_reasons(self, monkeypatch):
        def boom(prompt, **kw):
            raise LLMError("claude timed out after 180s")

        monkeypatch.setattr("grill.triage.complete", boom)
        transport_verdict = triage(session("why an idempotency key?"))

        monkeypatch.setattr(
            "grill.triage.complete", lambda prompt, **kw: completion("not json at all")
        )
        parse_verdict = triage(session("why an idempotency key?"))

        assert not transport_verdict.kept
        assert not parse_verdict.kept
        assert transport_verdict.reason != parse_verdict.reason


class TestFailureIsSilent:
    def test_no_turns_short_circuits_without_calling_the_model(self):
        v = triage(session())
        assert not v.kept
        assert v.cost_usd is None
        assert "stage 0 floor" in v.reason


class TestJSONExtraction:
    def test_bare_object(self):
        assert extract_json_object('{"verdict":"ask"}')["verdict"] == "ask"

    def test_fenced_object_with_preamble(self):
        text = 'Here you go:\n```json\n{"verdict":"silent","topic":null}\n```\n'
        assert extract_json_object(text)["verdict"] == "silent"

    def test_nested_object_is_not_truncated_by_brace_matching(self):
        text = 'noise {"verdict":"ask","meta":{"a":{"b":1}},"topic":"x"} trailing'
        assert extract_json_object(text)["topic"] == "x"

    def test_braces_inside_strings_do_not_confuse_the_matcher(self):
        text = '{"verdict":"ask","quote":"i ran {broken} and it failed","topic":"x"}'
        assert extract_json_object(text)["quote"] == "i ran {broken} and it failed"

    def test_no_object_raises(self):
        with pytest.raises(LLMError):
            extract_json_object("I could not determine a verdict.")

    def test_unescaped_quotes_raise_without_salvage_keys(self):
        with pytest.raises(LLMError):
            extract_json_object('{"verdict": "silent", "quote_shows": "she said "no" loudly"}')


class TestSalvage:
    """Regression tests from two real stage-1 responses that failed to parse.

    Both emitted unescaped quotation marks inside `reason`. This is not JSON and
    no prompt reliably prevents it, so the parser has to survive it — the field
    most likely to contain a quote is `quote`, which means strict parsing biases
    triage toward dropping the sessions it should keep.

    This exercises `extract_json_object`/`salvage_flat_object` directly, on the
    flat single-object shape they were built for. That shape no longer comes from
    triage's prompt — the array response has no salvage counterpart, deliberately
    (see `extract_json_array`'s docstring) — so the keys here are a plain local
    tuple rather than the deleted `VERDICT_KEYS`.
    """

    _KEYS = ("verdict", "signal", "topic", "quote", "quote_shows")

    def test_salvages_unescaped_quotes_in_reason(self):
        text = (
            '{\n  "verdict": "silent",\n  "signal": null,\n  "topic": null,\n'
            '  "quote": null,\n'
            '  "quote_shows": "prose revision of IDEA.md, with turns limited to '
            'directives ("revise IDEA.md", "yes", "leave it") — no curiosity."\n}'
        )
        data = extract_json_object(text, salvage_keys=self._KEYS)
        assert data["verdict"] == "silent"
        assert data["topic"] is None
        assert '"revise IDEA.md"' in data["quote_shows"]

    def test_salvages_an_ask_whose_quote_contains_quotes(self):
        text = (
            '{"verdict": "ask", "signal": "asked_why", "topic": "retry safety", '
            '"quote": "why does the "retry" wrapper not double-charge ?", '
            '"quote_shows": "developer asked why"}'
        )
        data = extract_json_object(text, salvage_keys=self._KEYS)
        assert data["verdict"] == "ask"
        assert data["quote"] == 'why does the "retry" wrapper not double-charge ?'
        assert data["quote_shows"] == "developer asked why"

    def test_well_formed_json_never_reaches_salvage(self):
        data = extract_json_object(
            '{"verdict":"ask","signal":"asked_why","topic":"x","quote":"a, b","quote_shows":"r"}',
            salvage_keys=self._KEYS,
        )
        assert data["quote"] == "a, b"


class TestSalvageBareNumbers:
    """`turn` in the moments schema is a bare JSON integer, not a string.

    Without this, per-element array salvage recovers `quote`/`topic`/`shows`
    from a broken element and then throws the whole moment away for want of
    an identity — the prompt always asks for `turn` unquoted, so this is the
    common case in practice, not an edge case.
    """

    def test_recovers_a_bare_integer(self):
        data = salvage_flat_object(
            '{"turn": 3, "quote": "she said "no" loudly"}', ("turn", "quote")
        )
        assert data["turn"] == 3
        assert isinstance(data["turn"], int)

    def test_recovers_a_bare_negative_float(self):
        data = salvage_flat_object('{"turn": -2.5, "quote": "ok"}', ("turn", "quote"))
        assert data["turn"] == -2.5
        assert isinstance(data["turn"], float)

    def test_still_skips_a_key_whose_value_is_not_a_recognized_shape(self):
        # No general permissive parsing: an array/object value for a key
        # stays unrecovered, exactly as before this change.
        data = salvage_flat_object('{"turn": [1,2], "quote": "ok"}', ("turn", "quote"))
        assert "turn" not in data
        assert data["quote"] == "ok"


class TestExtractJsonArray:
    def test_parses_a_bare_array(self):
        assert extract_json_array('[{"turn": 3}]') == [{"turn": 3}]

    def test_parses_a_fenced_array_with_preamble(self):
        text = 'Here are the moments:\n```json\n[{"turn": 3}, {"turn": 7}]\n```'
        assert extract_json_array(text) == [{"turn": 3}, {"turn": 7}]

    def test_parses_an_empty_array(self):
        assert extract_json_array("[]") == []

    def test_survives_a_bracket_inside_a_string_value(self):
        # Developer turns are rendered as "[7] text", so quotes routinely
        # contain brackets. Naive bracket matching truncates the array here.
        text = '[{"quote": "why does [7] matter?"}]'
        assert extract_json_array(text) == [{"quote": "why does [7] matter?"}]

    def test_discards_non_object_elements(self):
        assert extract_json_array('[{"turn": 1}, "junk", 5]') == [{"turn": 1}]

    def test_raises_when_there_is_no_array(self):
        with pytest.raises(LLMError):
            extract_json_array("I could not find anything worth asking about.")


# One element with an unescaped quote inside `quote`, flanked by two well-formed
# elements. Regression fixture for the per-element salvage fix: the whole array
# fails whole-string json.loads, but only the middle element should be lost
# without salvage_keys, and none should be lost with it.
BROKEN_MOMENTS_ARRAY = (
    '['
    '{"turn": 0, "signal": "asked_why", "topic": "idempotency keys", '
    '"quote": "why an idempotency key?", "shows": "asks why"}, '
    '{"turn": "1", "signal": "pushed_back", "topic": "retries", '
    '"quote": "no, use the "retry" wrapper instead", "shows": "corrects the approach"}, '
    '{"turn": 2, "signal": "asked_why", "topic": "ledgers", '
    '"quote": "why a ledger?", "shows": "asks why"}'
    "]"
)


class TestExtractJsonArraySalvage:
    def test_stays_strict_without_salvage_keys(self):
        # Unchanged behavior: no salvage_keys means one bad element still sinks
        # the whole array, exactly like before this fix.
        with pytest.raises(LLMError):
            extract_json_array(BROKEN_MOMENTS_ARRAY)

    def test_recovers_every_element_including_the_broken_one(self):
        moments = extract_json_array(BROKEN_MOMENTS_ARRAY, salvage_keys=MOMENT_KEYS)
        assert len(moments) == 3
        assert moments[0]["topic"] == "idempotency keys"
        assert moments[1]["turn"] == "1"
        assert moments[1]["quote"] == 'no, use the "retry" wrapper instead'
        assert moments[2]["topic"] == "ledgers"

    def test_recovers_a_broken_elements_unquoted_turn(self):
        # The realistic case: the prompt asks for `turn` as a bare integer,
        # so a degraded completion breaks `quote`'s escaping while `turn`
        # stays unquoted. Salvage must recover both, not just the text.
        quote = 'why does the "retry" wrapper not double-charge?'
        text = (
            '[{"turn": 1, "signal": "asked_why", "topic": "retries", '
            f'"quote": "{quote}", "shows": "asks why"}}]'
        )
        moments = extract_json_array(text, salvage_keys=MOMENT_KEYS)
        assert moments[0]["turn"] == 1
        assert moments[0]["quote"] == quote


def test_the_prompt_prefers_technical_mechanisms():
    """Prompt-only steering from the 2026-07-22 design: prefer moments whose
    core is a mechanism, down-rank behavioural or process moments."""
    from grill.triage import PROMPT

    assert "technical mechanism" in PROMPT
    assert "Down-rank" in PROMPT
