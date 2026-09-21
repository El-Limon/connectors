"""The stdlib KeyValues reader, against the document Steam actually serves."""

from __future__ import annotations

from pathlib import Path

import pytest

from takaro_maint.steam import vdf

FIXTURE = Path(__file__).parent / "fixtures/providers/steam/294420"

#: What steamcmd prints around the block: colour escapes, progress, the header line, and a
#: trailer after the closing brace. All of it has to be stepped over, none of it parsed.
NOISE_BEFORE = (
    "Redirecting stderr to '/root/Steam/logs/stderr.txt'\n"
    "[  0%] Checking for available updates...\n"
    "Steam Console Client (c) Valve Corporation - version 1758240593\n"
    "Logging in user 'anonymous' to Steam Public...\n"
    "Connecting anonymously to Steam Public...\x1b[0mOK\n"
    "\x1b[0mWaiting for user info...\x1b[0mOK\n"
    "\x1b[0m"
)
NOISE_AFTER = "Unloading Steam API...\x1b[0mOK\n\x1b[0m\n"


def capture(block: str | None = None) -> str:
    """A whole steamcmd run: the noise, the header line, a block, the trailer."""
    header = (FIXTURE / "header.txt").read_text(encoding="utf-8")
    if block is None:
        block = (FIXTURE / "app_info.vdf").read_text(encoding="utf-8")
    return NOISE_BEFORE + header + block + NOISE_AFTER


# -- T-V1 the grammar ----------------------------------------------------------
def test_parses_nested_keyvalues_with_tabs_quotes_and_escapes() -> None:
    text = (
        "// a leading comment\r\n"
        '"root"\r\n'
        "{\r\n"
        '\t"scalar"\t\t"plain"\r\n'
        '\t"escaped"\t\t"a \\"quoted\\" \\\\ word\\nline\\ttab"\r\n'
        "\tbare\t\tvalue // trailing comment\r\n"
        '\t"nested"\r\n'
        "\t{\r\n"
        '\t\t"deep"\t\t"1"\r\n'
        "\t}\r\n"
        "}\r\n"
    )

    document = vdf.parse(text)

    assert document == {
        "root": {
            "scalar": "plain",
            "escaped": 'a "quoted" \\ word\nline\ttab',
            "bare": "value",
            "nested": {"deep": "1"},
        }
    }


def test_last_duplicate_key_wins() -> None:
    assert vdf.parse('"k" "first" "k" "second"') == {"k": "second"}


# -- T-V2 finding the block in the noise ---------------------------------------
def test_extracts_the_app_block_from_steamcmd_noise() -> None:
    block, header = vdf.extract_app(capture(), 294420)

    assert block["common"]["name"] == "7 Days to Die Dedicated Server"
    assert block["depots"]["branches"]["public"]["buildid"] == "24994542"
    assert block["depots"]["294422"]["manifests"]["public"]["gid"] == "1633674551820196085"
    assert header == {"changeNumber": 39026857, "lastChange": "Mon Sep 21 12:53:46 2026"}


def test_a_depot_without_manifests_and_a_scalar_among_the_depots_survive_parsing() -> None:
    """Steam mixes shapes under ``depots``: three shared depots and one bare scalar."""
    block, _ = vdf.extract_app(capture(), 294420)
    depots = block["depots"]

    assert depots["228983"] == {"config": {"oslist": "windows"}, "depotfromapp": "228980", "sharedinstall": "1"}
    assert "manifests" not in depots["228983"]
    assert depots["overridescddb"] == "1"
    assert depots["privatebranches"] == "1"


def test_a_branch_may_carry_no_timeupdated() -> None:
    block, _ = vdf.extract_app(capture(), 294420)

    assert "timeupdated" not in block["depots"]["branches"]["alpha12.5"]
    assert block["depots"]["branches"]["alpha12.5"]["timebuildupdated"] == "1440323524"


# -- T-V3 the truncation -------------------------------------------------------
def test_a_truncated_block_is_reported_as_no_branch_data() -> None:
    with pytest.raises(vdf.VdfError) as raised:
        vdf.extract_app(capture('"294420"\n{\n}\n'), 294420)

    assert "no branch data for 294420" in str(raised.value)


def test_a_capture_without_the_app_line_is_an_error() -> None:
    with pytest.raises(vdf.VdfError) as raised:
        vdf.extract_app(NOISE_BEFORE + "No app info for AppID 294420 found, requesting...\n", 294420)

    assert "no block for 294420" in str(raised.value)


def test_another_apps_block_is_not_mistaken_for_this_one() -> None:
    other = capture().replace('"294420"\n{', '"999999"\n{', 1)

    with pytest.raises(vdf.VdfError):
        vdf.extract_app(other, 294420)


# -- T-V4 malformed documents --------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        '"root"\n{\n\t"k"\t"v"\n',  # never closed
        '"root"\n{\n}\n}\n',  # closed twice
        '"root"\n{\n\t"dangling"\n}\n',  # a key with no value
        '"unterminated',  # EOF inside a string
    ],
)
def test_unbalanced_or_dangling_input_is_an_error(text: str) -> None:
    with pytest.raises(vdf.VdfError):
        vdf.parse(text)


# -- T-V5 the inverse ----------------------------------------------------------
def test_dump_roundtrips_the_recorded_document() -> None:
    text = (FIXTURE / "app_info.vdf").read_text(encoding="utf-8")
    parsed = vdf.parse(text)

    assert vdf.parse(vdf.dump(parsed)) == parsed
    assert vdf.dump(parsed) == text, "the fixture is written in the same spelling dump() produces"


def test_dump_escapes_what_parse_unescapes() -> None:
    document = {"weird": {'a "key"': "tab\there\nand a \\ backslash"}}

    assert vdf.parse(vdf.dump(document)) == document
