"""
test_code_sets_database.py — iso20022_code_sets against the real database, as the
iso20022_mcp role.

Skipped when no DATABASE_URL is configured. They run on every push to main and nightly.
No -s needed: the two long tests print a dot per code set and the elapsed time while they run, so you can see
pytest is working. If a call gets no answer for CALL_TIMEOUT seconds the test fails and says so (stuck).

Release checklist references are in square brackets, e.g. [3.2].
"""
import asyncio
import json
import re
import time

import pytest

import code_sets
import support
from mcp import Client

pytestmark = pytest.mark.skipif(not support.HAS_DB, reason="no DATABASE_URL: database checks skipped")

TOOL = support.CODE_SETS_TOOL
BUDGET_CHARS = 20_000          # [3.2] raised from 16,000 on 2026-10-12 (the mappings tool keeps 16,000); about 7,000 tokens
MIN_ROWS_NON_FINAL = 10        # a non-final page with fewer rows means the budget has collapsed
MAX_ROW_CHARS = 2_000          # the longest value row is about 900 characters; far below any page budget
CALL_TIMEOUT = 60              # a single call with no answer for this long is treated as stuck
MAX_SECONDS = 3.0              # [5.3] per call, after a warm-up call
TOP_MESSAGES = 50              # messages with the most code sets
TOP_CODES = 20                 # codes found in the most code sets
BTC = [c.lower() for c in code_sets.BTC_CONCEPTS]
MESSAGE_PATTERN = re.compile(r"^[A-Za-z0-9 .\-/()_]+$")

# [1.6] Attack strings that pass the input patterns and so reach the SQL layer.
PATTERN_SAFE_ATTACKS = [
    "BalanceType OR 1-1",
    "BalanceType) OR (1-1",
    "1) OR (1",
    "BalanceType--",
    "CLBD UNION SELECT 1",
    "camt.053.001",
    "camt.053 OR 1",
    "....",
    "____",
]


class Progress:
    """Prints a dot per item (and a count with elapsed seconds every 10) straight to the terminal, past pytest's
    output capture, so a long test shows that it is running."""

    def __init__(self, request, label: str):
        self.capman = request.config.pluginmanager.getplugin("capturemanager")
        self.label = label

    def __enter__(self):
        self.suspended = self.capman.global_and_fixture_disabled()
        self.suspended.__enter__()
        self.start = time.monotonic()
        print(f"\n{self.label}\n  ", end="", flush=True)
        return self.tick

    def tick(self, done: int, total: int) -> None:
        print(".", end="", flush=True)
        if done % 10 == 0 or done == total:
            print(f" {done}/{total} ({time.monotonic() - self.start:.0f}s)\n  ", end="", flush=True)

    def __exit__(self, *exc):
        print(f"done in {time.monotonic() - self.start:.0f}s", flush=True)
        self.suspended.__exit__(*exc)


def walk_all(arg_sets: list[dict], tick=None) -> list[list]:
    """For each argument dict, every page of the response, following next_offset. tick(done, total) is called
    after each argument dict."""
    async def go():
        out = []
        async with Client(support.server.mcp) as client:
            for args in arg_sets:
                pages, offset = [], 0
                while True:
                    try:
                        result = await asyncio.wait_for(
                            client.call_tool(TOOL, args | ({"offset": offset} if offset else {})), CALL_TIMEOUT)
                    except TimeoutError:
                        pytest.fail(f"STUCK: no answer for {CALL_TIMEOUT}s from {args} offset {offset}")
                    assert not result.is_error, f"{args}: {result.content[0].text}"
                    pages.append(result)
                    page = result.structured_content["page"]
                    if page["complete"]:
                        break
                    assert page["next_offset"] > offset, f"{args}: paging did not advance"
                    offset = page["next_offset"]
                    assert len(pages) < 200, f"{args}: paging does not end"
                out.append(pages)
                if tick:
                    tick(len(out), len(arg_sets))
        return out
    return asyncio.run(go())


def ask(message: str, **extra) -> dict:
    [result] = support.call_tool({"message": message, **extra}, tool=TOOL)
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def rows(body: dict, field: str) -> list[str]:
    return body[field].split("\n")[1:]


def check_pages(label: str, pages: list, field: str) -> int:
    """[3.2] Budget on every page, no collapsed non-final page, no repeated row. Returns the total row count."""
    seen, total = set(), 0
    for i, result in enumerate(pages):
        size = len(result.content[0].text)
        assert size <= BUDGET_CHARS, f"{label} page {i + 1}: {size:,} characters"
        body = result.structured_content
        page_rows = rows(body, field)
        assert body["page"]["returned"] == len(page_rows), label
        if i < len(pages) - 1:
            assert len(page_rows) >= MIN_ROWS_NON_FINAL, f"{label} page {i + 1} has only {len(page_rows)} rows"
        assert not seen & set(page_rows), f"{label}: a row appears on two pages"
        seen |= set(page_rows)
        total += len(page_rows)
    assert pages[-1].structured_content["page"]["total"] == total, label
    return total


@pytest.fixture(scope="module")
def concepts() -> list[str]:
    found = support.db.fetch_all(
        "SELECT concept FROM i22_codeset WHERE lower(concept) <> ALL(%s) ORDER BY concept", (BTC,))
    assert len(found) > 100
    return [r["concept"] for r in found]


@pytest.fixture(scope="module")
def value_counts() -> dict[str, int]:
    found = support.db.fetch_all("SELECT concept, count(*) AS n FROM i22_code_value GROUP BY concept")
    return {r["concept"]: r["n"] for r in found}


@pytest.fixture(scope="module")
def code_set_pages(request, concepts) -> dict[str, list]:
    """Every page of every non-BTC code set, fetched once and shared by the tests that need it."""
    with Progress(request, f"Fetching all {len(concepts)} code sets (one dot each):") as tick:
        results = walk_all([{"message": c} for c in concepts], tick)
    return dict(zip(concepts, results, strict=True))


# ---- every code set ------------------------------------------------------------------------

def test_every_code_set_within_budget(code_set_pages, value_counts, capsys):
    """[3.2][7.3] Every non-BTC code set, every page: within budget, no collapsed pages, and the rows add up
    to every value (current and deactivated)."""
    biggest = []
    for concept, pages in code_set_pages.items():
        first = pages[0].structured_content
        assert first["found"] is True and first["code_set"]["concept"] == concept
        total = check_pages(concept, pages, "values")
        assert total == value_counts.get(concept, 0), f"{concept}: {total} rows, {value_counts.get(concept, 0)} values"
        counted = first["code_set"]["active_values"] + first["code_set"].get("deactivated_values", 0)
        assert counted == total, f"{concept}: counts in code_set do not match the rows"
        biggest.append((max(len(p.content[0].text) for p in pages), len(pages), concept))
    biggest.sort(reverse=True)
    with capsys.disabled():
        print("\nLargest code set responses (characters, pages):")
        for size, n_pages, concept in biggest[:10]:
            print(f"  {concept}: {size:,} in {n_pages} page(s)")
        print(f"  budget: {BUDGET_CHARS:,}")


def test_used_in_messages_matches_the_database(code_set_pages):
    expected = {r["concept"]: r["n"] for r in support.db.fetch_all(
        "SELECT concept, count(DISTINCT message_id) AS n FROM i22_codeset_message GROUP BY concept")}
    for concept, pages in code_set_pages.items():
        got = len(pages[0].structured_content.get("used_in_messages", []))
        assert got == expected.get(concept, 0), f"{concept}: {got} messages, expected {expected.get(concept, 0)}"
        for later in pages[1:]:
            assert later.structured_content.get("used_in_messages") == pages[0].structured_content.get(
                "used_in_messages"), f"{concept}: the message list differs between pages"


# ---- messages and codes --------------------------------------------------------------------

def test_messages_with_the_most_code_sets_within_budget(request):
    top = support.db.fetch_all(
        "SELECT message_id FROM i22_codeset_message WHERE variant = 1 "
        "GROUP BY message_id ORDER BY count(*) DESC, message_id LIMIT %s", (TOP_MESSAGES,))
    ids = [r["message_id"] for r in top]
    expected = {r["message_id"]: r["n"] for r in support.db.fetch_all(
        "SELECT message_id, count(*) AS n FROM i22_codeset_message WHERE variant = 1 GROUP BY message_id")}
    with Progress(request, f"Fetching the {len(ids)} messages with the most code sets:") as tick:
        results = walk_all([{"message": m} for m in ids], tick)
    for message_id, pages in zip(ids, results, strict=True):
        assert pages[0].structured_content["found"] is True, message_id
        assert check_pages(message_id, pages, "code_sets") == expected[message_id], message_id


def test_codes_in_the_most_code_sets_within_budget(request):
    top = support.db.fetch_all(
        "SELECT code, count(*) AS n FROM i22_code_value WHERE lower(concept) <> ALL(%s) "
        "GROUP BY code HAVING count(*) > 1 ORDER BY count(*) DESC, code LIMIT %s", (BTC, TOP_CODES))
    wanted = [(r["code"], r["n"]) for r in top if MESSAGE_PATTERN.match(r["code"])]
    assert wanted
    with Progress(request, f"Fetching the {len(wanted)} codes found in the most code sets:") as tick:
        results = walk_all([{"message": c} for c, _ in wanted], tick)
    for (code, n), pages in zip(wanted, results, strict=True):
        body = pages[0].structured_content
        assert body["input_kind"] in ("code", "code_set"), code      # a code that is also a code set name wins as the set
        if body["input_kind"] == "code":
            assert check_pages(code, pages, "matches") == n, code


def test_known_shapes():
    """Data facts: BalanceType holds CLBD; every part of the three kinds answers."""
    full_name = support.db.fetch_all(
        "SELECT codeset_name FROM i22_codeset WHERE concept = 'BalanceType'")[0]["codeset_name"]
    by_name = ask(full_name)
    assert by_name["input"] == "BalanceType" and by_name["found"] is True
    one = ask("BalanceType", code="CLBD")
    assert one["found"] is True and rows(one, "values")[0].startswith("CLBD|")
    wrong = ask("BalanceType", code="NOPE")
    assert wrong["found"] is False and wrong["notes"]
    code = ask("CLBD")
    assert code["input_kind"] == "code" and any(r.startswith("BalanceType|CLBD|") for r in rows(code, "matches"))
    assert ask("camt.053")["input_kind"] == "message"
    assert ask("Purpose")["page"]["complete"] is False, "Purpose (332 values) is expected to need paging"


def test_btc_concepts_are_pointed_to_the_btc_tool():
    for name in code_sets.BTC_CONCEPTS:
        body = ask(name)
        assert body["found"] is False
        assert any("iso20022_bank_transaction_codes" in n for n in body["notes"]), name


def test_deactivated_value_is_flagged_not_hidden():
    found = support.db.fetch_all(
        "SELECT concept, code FROM i22_code_value WHERE deactivated_in IS NOT NULL AND lower(concept) <> ALL(%s) "
        "ORDER BY concept, code LIMIT 1", (BTC,))
    if not found:
        pytest.skip("no deactivated code values in this data")
    body = ask(found[0]["concept"], code=found[0]["code"])
    assert body["found"] is True
    cells = rows(body, "values")[0].split("|")
    assert cells[0] == found[0]["code"] and cells[3], "deactivated_in is empty for a deactivated value"
    assert any(n.startswith("This page includes deactivated values") for n in body["notes"])


# ---- the data the format relies on ---------------------------------------------------------

def test_no_value_contains_a_pipe_or_other_row_breaker():
    """The row format is safe because no value holds a pipe, tab or carriage return. If a data load adds one,
    this fails and the format needs another look (the tool would still replace it with a space)."""
    [r] = support.db.fetch_all(
        "SELECT count(*) FILTER (WHERE strpos(txt, '|') > 0) AS pipes, "
        "       count(*) FILTER (WHERE strpos(txt, chr(9)) > 0) AS tabs, "
        "       count(*) FILTER (WHERE strpos(txt, chr(13)) > 0) AS carriage_returns, "
        "       max(length(txt)) AS longest "
        "FROM (SELECT concat_ws(' ', code, code_name, description) AS txt FROM i22_code_value "
        "      WHERE lower(concept) <> ALL(%s)) t", (BTC,))
    assert (r["pipes"], r["tabs"], r["carriage_returns"]) == (0, 0, 0)
    assert r["longest"] <= MAX_ROW_CHARS, f"longest row is {r['longest']} characters"


# ---- input safety [1.6] --------------------------------------------------------------------

@pytest.mark.parametrize("attack", PATTERN_SAFE_ATTACKS)
def test_injection_returns_nothing(attack):
    """[1.6] Attack strings return not-found or a validation error, never extra rows or a database error."""
    for args in ({"message": attack}, {"message": "BalanceType", "code": attack}):
        [result] = support.call_tool(args, tool=TOOL)
        if result.is_error:
            assert "validation error" in result.content[0].text, result.content[0].text
            continue
        assert json.loads(result.content[0].text)["found"] is False, f"rows returned for {args}"


def test_calls_are_fast():
    """[5.3] Each call completes well inside the statement timeout."""
    ask("BalanceType")                                      # warm-up: wakes an idle database
    for message in ("BalanceType", "Purpose", "OTHR", "camt.053", "NoSuchThing"):
        start = time.monotonic()
        ask(message)
        elapsed = time.monotonic() - start
        assert elapsed < MAX_SECONDS, f"{message}: {elapsed:.2f}s"
