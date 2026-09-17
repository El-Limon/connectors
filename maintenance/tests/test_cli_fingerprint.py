"""The fingerprint contract, against the fixture the Gradle twin also reads."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from takaro_maint.fingerprint import canonical, fingerprint, fp16, subset

FIXTURE = Path(__file__).parent / "fixtures" / "fingerprints.json"


def cases() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", cases(), ids=lambda case: case["name"])
def test_every_fixture_case_reproduces_its_fingerprint(case: dict[str, Any]) -> None:
    assert fingerprint(case["record"]) == case["fingerprint"]


def test_the_fixture_covers_the_cases_the_gradle_twin_needs() -> None:
    names = {case["name"] for case in cases()}

    assert "minecraft/fabric-26.2 (the real record)" in names
    assert "nested unicode and control characters" in names
    assert "sorted twin" in names
    assert "the same record with every key reordered" in names


def test_key_order_does_not_change_the_fingerprint() -> None:
    by_name = {case["name"]: case for case in cases()}

    assert by_name["sorted twin"]["fingerprint"] == by_name["the same record with every key reordered"]["fingerprint"]


def test_a_changed_digest_changes_the_fingerprint() -> None:
    by_name = {case["name"]: case for case in cases()}

    assert (
        by_name["minecraft/fabric-26.2 (the real record)"]["fingerprint"]
        != by_name["fabric-26.2 with a different container digest"]["fingerprint"]
    )


def test_support_metadata_stays_outside_the_fingerprint() -> None:
    by_name = {case["name"]: case for case in cases()}

    assert (
        by_name["minecraft/fabric-26.2 (the real record)"]["fingerprint"]
        == by_name["fabric-26.2 with different support notes and default flag"]["fingerprint"]
    )


def test_the_fingerprint_only_covers_the_documented_keys() -> None:
    record = next(case["record"] for case in cases() if case["name"].startswith("minecraft/fabric-26.2"))

    assert set(subset(record)) == {"id", "game", "platform", "revision", "inputs", "runtime", "build"}


def test_fp16_is_the_first_sixteen_characters() -> None:
    record = next(case["record"] for case in cases() if case["name"].startswith("minecraft/fabric-26.2"))

    assert fp16(record) == fingerprint(record)[:16]
    assert fp16(fingerprint(record)) == fingerprint(record)[:16]


def test_canonical_json_has_no_whitespace_and_sorted_keys() -> None:
    text = canonical({"b": 1, "a": {"d": [1, 2], "c": "x"}})

    assert text == '{"a":{"c":"x","d":[1,2]},"b":1}'


def test_canonical_json_leaves_solidus_and_non_ascii_unescaped() -> None:
    text = canonical({"path": "/v2/versions", "name": "café"})

    assert "/v2/versions" in text
    assert "café" in text


def test_canonical_json_refuses_floats() -> None:
    with pytest.raises(TypeError):
        canonical({"value": 1.5})


def test_a_changed_input_hash_changes_the_fingerprint() -> None:
    record = next(case["record"] for case in cases() if case["name"].startswith("minecraft/fabric-26.2"))
    tampered = copy.deepcopy(record)
    tampered["inputs"]["fabricApi"]["sha256"] = "0" * 64

    assert fingerprint(tampered) != fingerprint(record)
