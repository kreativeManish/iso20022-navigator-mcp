"""
test_message_details_database.py — iso20022_message_details against the real database, as the
iso20022_mcp role.

Skipped when no DATABASE_URL is configured. They run on every push to main and nightly.
Run with -s to see the size table printed by test_largest_responses_within_budget.

The shared lookup limit is 600 calls a minute, so these checks ask the tool about a sample of
messages and compare against direct queries for the whole catalogue.

Release checklist references are in square brackets, e.g. [3.2].
"""
import json
import time

import pytest

import message_details as md
import support

pytestmark = pytest.mark.skipif(not support.HAS_DB, reason="no DATABASE_URL: database checks skipped")

TOOL = support.MESSAGE_TOOL
BUDGET_CHARS = 2_500           # [3.2] Measured 2026-10-11 in CI: largest is catm.005 at 1,786 characters. 2,500 leaves about 40% headroom for a data refresh.
                               # test_largest_responses_within_budget gives the real maximum; tighten to it plus headroom.
MAX_SECONDS = 3.0              # [5.3] per call, after a warm-up call
SAMPLE = 40                    # messages asked per sampled check (well inside the 600-per-minute limit)

# [1.6] Attack strings that pass the input pattern (four letters, three digits) and so reach the reader.
PATTERN_SAFE_ATTACKS = [
    "drop 001",
    "unio-001",
    "null.000",
    "true_111",
    "or1 1=1",
    "    pacs   008    ",
    "pacs.008.001.13 ",
]


def ask(*ids: str) -> list:
    return support.call_tool(*({"message_id": i} for i in ids), tool=TOOL)


def body(result) -> dict:
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def rows(sql: str, params=()):
    return support.db.fetch_all(sql, params)


@pytest.fixture(scope="module")
def held() -> list[dict]:
    out = rows("SELECT message_id, message_type, version, deactivated_in FROM i22_message_type "
               "WHERE variant = 1 ORDER BY message_id")
    assert len(out) > 100
    return out


@pytest.fixture(scope="module")
def sample(held) -> list[str]:
    """Evenly spread across the catalogue, so every business area is likely to appear."""
    step = max(1, len(held) // SAMPLE)
    return [r["message_id"] for r in held[::step]][:SAMPLE]


# ---- the data the tool relies on ---------------------------------------------------------

def test_every_variant_one_message_has_the_expected_shape(held):
    """The reader and the not-found search rely on 'abcd.123' ids and a version that matches the full id."""
    for r in held:
        assert len(r["message_id"]) == 8 and r["message_id"][4] == ".", r["message_id"]
        assert r["message_type"].startswith(r["message_id"] + ".001."), r["message_type"]
        assert r["version"] == int(r["message_type"][-2:]), r["message_type"]


def test_every_message_area_has_a_business_area_row(held):
    [missing] = rows("SELECT count(*) AS n FROM i22_message_type m LEFT JOIN i22_business_area b "
                     "ON b.code = left(m.message_id, 4) WHERE m.variant = 1 AND b.code IS NULL")
    assert missing["n"] == 0, "a message without a business area is returned without one"


# ---- known messages ----------------------------------------------------------------------

@pytest.mark.parametrize("message_id, area", [("pacs.008", "pacs"), ("camt.053", "camt"), ("pain.001", "pain")])
def test_well_known_messages(message_id, area):
    b = body(ask(message_id)[0])
    assert b["found"] is True and b["input"] == message_id
    assert b["latest_version"].startswith(message_id + ".001.")
    assert b["business_area"]["code"] == area and b["business_area"]["name"] and b["business_area"]["domain"]
    assert b["name"] and b["description"].strip() and b["navigator_group"]
    assert b["structure"]["total_elements"] > 10 and b["code_set_count"] >= 0


def test_equivalent_input_forms_give_the_same_answer():
    forms = ["pacs.008", "PACS-008", "pacs_008", "pacs 008", "pacs008"]
    answers = [body(r) for r in ask(*forms)]
    assert all(a == answers[0] for a in answers)


def test_full_identifier_of_another_version_notes_the_difference(held):
    r = next(h for h in held if h["message_id"] == "pacs.008")
    other = 1 if r["version"] != 1 else 2
    b = body(ask(f"pacs.008.001.{other:02d}")[0])
    assert b["found"] is True and b["latest_version"] == r["message_type"]
    assert any("is not held" in n for n in b["notes"]), b.get("notes")


def test_retired_message_is_flagged(held):
    retired = [h["message_id"] for h in held if h["deactivated_in"]]
    if not retired:
        pytest.skip("no retired message in this data")
    b = body(ask(retired[0])[0])
    assert b["deactivated_in"] and any(b["deactivated_in"] in n for n in b["notes"]), b.get("notes")


# ---- consistency with the tables ---------------------------------------------------------

def test_structure_and_code_set_counts_match_the_tables(sample):
    """Sampled messages: the tool's figures equal direct counts."""
    for message_id, result in zip(sample, ask(*sample), strict=True):
        b = body(result)
        [s] = rows("SELECT count(*) AS n, coalesce(max(depth), 0) AS deepest FROM i22_message_structure "
                   "WHERE message_id = %s AND variant = 1", (message_id,))
        if s["n"]:
            assert b["structure"]["total_elements"] == s["n"], message_id
            assert b["structure"]["max_depth"] == s["deepest"], message_id
            by_depth = b["structure"].get("elements_by_depth", {})
            assert all(k.isdigit() and int(k) >= 2 for k in by_depth), message_id
            assert sum(by_depth.values()) == s["n"] - sum(
                x["n"] for x in rows("SELECT depth, count(*) AS n FROM i22_message_structure WHERE message_id = %s "
                                     "AND variant = 1 AND depth < 2 GROUP BY depth", (message_id,))), message_id
        else:
            assert "structure" not in b, message_id
        [c] = rows("SELECT count(*) AS n FROM i22_codeset_message WHERE message_id = %s AND variant = 1",
                   (message_id,))
        assert b["code_set_count"] == c["n"], message_id


# ---- not found ---------------------------------------------------------------------------

def test_not_found_in_a_real_area_suggests_held_messages(held):
    ids = {h["message_id"] for h in held}
    b = body(ask("pacs.999")[0])
    assert "pacs.999" not in ids
    assert b["found"] is False
    assert 0 < len(b["closest_ids"]) <= md.MAX_CLOSEST_IDS
    assert all(i in ids and i.startswith("pacs.") for i in b["closest_ids"])
    assert any("outside this dataset" in n for n in b["notes"])


def test_not_found_in_an_unknown_area():
    b = body(ask("zzzz.001")[0])
    assert b["found"] is False and "closest_ids" not in b
    assert any("business area with the code 'zzzz'" in n for n in b["notes"])


@pytest.mark.parametrize("attack", PATTERN_SAFE_ATTACKS)
def test_injection_returns_nothing(attack):
    """[1.6] Attack strings return not-found or an error message, never extra rows or a database error."""
    [result] = ask(attack)
    if result.is_error:
        text = result.content[0].text
        assert "Accepted forms" in text or "validation error" in text, text
        return
    b = body(result)
    assert b["found"] is False or b["input"] == "pacs.008", f"unexpected rows for {attack!r}"


# ---- result size and speed ---------------------------------------------------------------

def test_largest_responses_within_budget(sample, capsys):
    """[3.2] The response grows with the description length, the depth array and the notes. Ask for the deepest
    structures, the longest descriptions, a retired message, a version difference and a not-found."""
    deepest = [r["message_id"] for r in rows(
        "SELECT message_id FROM i22_message_structure WHERE variant = 1 GROUP BY message_id "
        "ORDER BY max(depth) DESC, message_id LIMIT 15")]
    longest = [r["message_id"] for r in rows(
        "SELECT message_id FROM i22_message_type WHERE variant = 1 "
        "ORDER BY length(rich_description) DESC NULLS LAST, message_id LIMIT 15")]
    asked = list(dict.fromkeys(deepest + longest + sample + ["pacs.999", "pacs.008.002.99"]))
    results = ask(*asked)
    sizes = sorted(((len(r.content[0].text), i) for i, r in zip(asked, results, strict=True)), reverse=True)
    with capsys.disabled():
        print("\nLargest message-details responses (characters):")
        for size, message_id in sizes[:10]:
            print(f"  {message_id}: {size:,}")
        print(f"  budget: {BUDGET_CHARS:,}")
    size, message_id = sizes[0]
    assert size <= BUDGET_CHARS, f"{message_id}: {size:,} characters"


def test_calls_are_fast():
    """[5.3] Each call completes well inside the statement timeout, including the deepest structure."""
    [deepest] = [r["message_id"] for r in rows(
        "SELECT message_id FROM i22_message_structure WHERE variant = 1 GROUP BY message_id "
        "ORDER BY count(*) DESC, message_id LIMIT 1")]
    ask("pacs.008")                                          # warm-up: wakes an idle database
    for message_id in ("pacs.008", "camt.053", deepest, "pacs.999", "zzzz.001"):
        start = time.monotonic()
        ask(message_id)
        elapsed = time.monotonic() - start
        assert elapsed < MAX_SECONDS, f"{message_id}: {elapsed:.2f}s"
