"""
test_bank_transaction_codes_database.py — iso20022_bank_transaction_codes against the real
database, as the iso20022_mcp role.

Skipped when no DATABASE_URL is configured. They run on every push to main and nightly.
Run with -s to see the size table printed by test_largest_responses_within_budget.

Release checklist references are in square brackets, e.g. [3.2].
"""
import json
import time

import pytest

import bank_transaction_codes as btc
import support

pytestmark = pytest.mark.skipif(not support.HAS_DB, reason="no DATABASE_URL: database checks skipped")

TOOL = support.BTC_TOOL
BUDGET_CHARS = 6_000           # [3.2] PROVISIONAL: set from the measured table, with headroom
MAX_SECONDS = 3.0              # [5.3] per call, after a warm-up call
CONCEPTS = ("BankTransactionDomain", "BankTransactionFamily", "BankTransactionSubFamily")

# [1.6] Attack strings that pass the input pattern and so reach the code reader.
PATTERN_SAFE_ATTACKS = [
    "PMNT OR 1",
    "PMNT-RCDT-ESCT-OR-1",
    "1 OR 1",
    "PMNT--",
    "PMNT UNION SELECT 1",
    "....",
    "____",
    "/ / /",
    "PMNT/RCDT/ESCT/",
]


def ask(*codes: str) -> list:
    return support.call_tool(*({"code": c} for c in codes), tool=TOOL)


def body(result) -> dict:
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


@pytest.fixture(scope="module")
def all_codes() -> dict[str, list[str]]:
    rows = support.db.fetch_all(
        "SELECT concept, code FROM i22_code_value WHERE concept = ANY(%s) ORDER BY concept, code", (list(CONCEPTS),))
    out = {c: [r["code"] for r in rows if r["concept"] == c] for c in CONCEPTS}
    assert all(out.values())
    return out


def test_code_counts_match_the_design_document(all_codes):
    """Data facts recorded in the i22 database design document."""
    assert [len(all_codes[c]) for c in CONCEPTS] == [11, 61, 284]


def test_domains_account_for_every_combination(all_codes):
    results = ask(*all_codes["BankTransactionDomain"])
    total = 0
    for code, result in zip(all_codes["BankTransactionDomain"], results):
        m = body(result)["matches"][0]
        assert m["level"] == "domain" and m["resolved"]["domain"]["code"] == code
        assert m["resolved"]["domain"]["name"]
        total += m["counts"]["combinations"]
    assert total == 1564


def test_families_account_for_every_combination(all_codes):
    """Every combination has exactly one family, so the family totals add up to the whole list."""
    results = ask(*("/" + c for c in all_codes["BankTransactionFamily"]))
    total = 0
    for result in results:
        m = body(result)["matches"][0]
        assert m["level"] == "family" and m["resolved"]["family"]["name"]
        total += m["counts"]["combinations"]
    assert total == 1564


def test_known_shapes():
    pmnt, rcdt = (body(r)["matches"][0] for r in ask("PMNT", "/RCDT"))
    assert len(pmnt["families"]) == 19, "PMNT should have 19 families (database design analysis)"
    assert rcdt["counts"]["subfamilies"] == 41, "RCDT should have 41 subfamilies (database design analysis)"


def test_single_path_code_carries_its_description():
    """IADD is recorded only under PMNT-MDOP, so the lookup resolves to one combination."""
    m = body(ask("IADD")[0])["matches"][0]
    assert m["counts"]["combinations"] == 1
    assert m["description"].strip()
    full = body(ask("PMNT-MDOP-IADD")[0])["matches"][0]
    assert full["level"] == "combination" and full["description"] == m["description"]


@pytest.mark.parametrize("code, levels", [
    ("CASH", ["family", "subfamily"]), ("NTAV", ["family", "subfamily"]), ("OPTN", ["family", "subfamily"]),
    ("OTHR", ["family", "subfamily"]), ("SWAP", ["family", "subfamily"]), ("TRAD", ["domain", "subfamily"]),
])
def test_codes_that_exist_at_two_levels(code, levels):
    b = body(ask(code)[0])
    assert [m["level"] for m in b["matches"]] == levels
    assert any("more than one level" in n for n in b["notes"])


def test_every_combination_has_a_description_and_names():
    """Joins are inner joins: a missing code row would silently drop combinations."""
    [row] = support.db.fetch_all("SELECT count(*) AS n FROM i22_btc_combinations")
    [joined] = support.db.fetch_all(
        "SELECT count(*) AS n FROM i22_btc_combinations c "
        "JOIN i22_code_value dn ON dn.concept = 'BankTransactionDomain' AND dn.code = c.domain "
        "JOIN i22_code_value fn ON fn.concept = 'BankTransactionFamily' AND fn.code = c.family "
        "JOIN i22_code_value sn ON sn.concept = 'BankTransactionSubFamily' AND sn.code = c.subfamily")
    assert row["n"] == joined["n"] == 1564
    [empty] = support.db.fetch_all(
        "SELECT count(*) AS n FROM i22_btc_combinations WHERE description IS NULL OR btrim(description) = ''")
    assert empty["n"] == 0


@pytest.mark.parametrize("attack", PATTERN_SAFE_ATTACKS)
def test_injection_returns_nothing(attack):
    """[1.6] Attack strings return not-found or an error message, never extra rows or a database error."""
    [result] = ask(attack)
    if result.is_error:
        text = result.content[0].text
        assert "Accepted forms" in text or "validation error" in text, text
        return
    assert body(result)["found"] is False, f"rows returned for {attack!r}"


def test_not_found_for_real_parts_in_an_unreal_pair():
    """RCDT is a PMNT family; no other domain has it with ESCT, so this pair is not a combination."""
    [row] = support.db.fetch_all(
        "SELECT count(*) AS n FROM i22_btc_combinations WHERE domain = 'ACMT' AND family = 'RCDT'")
    if row["n"]:
        pytest.skip("ACMT-RCDT exists in this data; pick another unreal pair")
    b = body(ask("ACMT-RCDT-ESCT")[0])
    assert b["found"] is False and b["matches"] == []
    assert [s["code"] for s in b["segments"]] == ["ACMT", "RCDT", "ESCT"]
    assert all(s.get("exists_as") for s in b["segments"])
    assert 0 < len(b["closest_paths"]) <= btc.MAX_CLOSEST_PATHS


def test_largest_responses_within_budget(all_codes, capsys):
    """[3.2] Every code sent bare is the widest question the tool takes, so the largest of them is the
    largest response there is. Also checks the item ceiling."""
    codes = [c for concept in CONCEPTS for c in all_codes[concept]]
    results = ask(*codes)
    sizes = sorted(((len(r.content[0].text), c) for c, r in zip(codes, results)), reverse=True)
    with capsys.disabled():
        print("\nLargest bare-code responses (characters):")
        for size, code in sizes[:10]:
            print(f"  {code}: {size:,}")
        print(f"  budget: {BUDGET_CHARS:,}")
    for result in results:
        data = result.structured_content
        listed = sum(len(m.get(k, [])) for m in data["matches"] for k in ("domains", "families", "subfamilies"))
        assert listed <= btc.MAX_LISTED_ITEMS
    size, code = sizes[0]
    assert size <= BUDGET_CHARS, f"{code}: {size:,} characters"


def test_calls_are_fast():
    """[5.3] Each call completes well inside the statement timeout."""
    ask("PMNT")                                             # warm-up: wakes an idle database
    for code in ("PMNT", "PMNT-RCDT-ESCT", "/MDOP", "OTHR", "ZZZZ", "PMNT-RCDT-IADD"):
        start = time.monotonic()
        ask(code)
        elapsed = time.monotonic() - start
        assert elapsed < MAX_SECONDS, f"{code}: {elapsed:.2f}s"
