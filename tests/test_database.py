"""
test_database.py — checks against the real database, as the iso20022_mcp role.

Skipped when no DATABASE_URL is configured (e.g. pull requests from forks, which
GitHub does not give secrets to). They run on every push to main and nightly.

Release checklist references are in square brackets, e.g. [8.6].
"""
import json
import time
from pathlib import Path

import psycopg
import pytest

import support

pytestmark = pytest.mark.skipif(not support.HAS_DB, reason="no DATABASE_URL: database checks skipped")

SNAPSHOT_FILE = Path(__file__).parent / "access_snapshot.sha256"
BUDGET_CHARS = 16_000          # [3.2] about 4,000 tokens; largest real response is ~9,500 characters
MAX_SECONDS = 3.0              # [5.3] per call, after a warm-up call
MAX_STATEMENT_TIMEOUT_MS = 30_000

# (message, standard, expected found)
CASES = [
    ("MT103", None, True),
    ("mt 103", None, True),
    ("103", None, True),
    ("MT940", None, True),
    ("ACH Statement", None, True),
    ("pacs.008", None, True),
    ("pacs.008.001.08", None, True),
    ("pacs.009", None, True),
    ("pain.008", "SEPA", True),
    ("tsin.004", None, False),       # deactivated: flagged, not hidden
    ("xyzz.999", None, False),
    ("MT999", None, False),
    ("iDEAL", None, False),
    ("pacs.008", "FEDWIRE", False),
]

# [1.6] Attack strings that pass the input pattern and so reach the SQL layer.
PATTERN_SAFE_ATTACKS = [
    "MT103 OR 1-1",
    "MT103) OR (1-1",
    "1) OR (1",
    "MT103--",
    "103 UNION SELECT 1",
    "pacs.008.001",
    "pacs.008 OR 1",
]


def _args(message, standard=None):
    return {"message": message} | ({"standard": standard} if standard else {})


def _body(result) -> dict:
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def _no_nulls(value, path="response"):
    """[3.4] Nulls and empty lists are omitted."""
    if isinstance(value, dict):
        for k, v in value.items():
            assert v is not None and v != [], f"{path}.{k} is empty"
            _no_nulls(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _no_nulls(v, f"{path}[{i}]")


@pytest.fixture(scope="module")
def case_results():
    return dict(zip(CASES, support.call_tool(*(_args(m, s) for m, s, _ in CASES))))


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c[0]}|{c[1]}")
def test_expected_answers(case_results, case):
    """[3.4][3.5][3.6][7.3] Expected found/not-found, explained not-found, provenance, no nulls."""
    message, standard, found = case
    body = _body(case_results[case])
    assert body["found"] is found
    assert body["provenance"]["data_baseline"]
    if not found:
        assert body.get("notes"), "not-found must explain itself"
    _no_nulls(body)


def test_deactivated_message_flagged(case_results):
    """[7.3]"""
    body = _body(case_results[("tsin.004", None, False)])
    assert body["iso20022_deactivated_in"]
    assert any("is a recognised ISO 20022 message that was retired" in n for n in body["notes"])


@pytest.mark.parametrize("attack", PATTERN_SAFE_ATTACKS)
def test_injection_returns_nothing(attack):
    """[1.6] Attack strings return not-found or a validation error, never extra rows or a database error.

    The strings pass the message pattern; some break the stricter standard pattern, and
    a validation error is an acceptable outcome there.
    """
    for args in (_args(attack), _args("MT103", attack)):
        [result] = support.call_tool(args)
        if result.is_error:
            assert "validation error" in result.content[0].text, result.content[0].text
            continue
        assert _body(result)["found"] is False, f"rows returned for {args}"


def test_qualifier_is_ignored_not_executed():
    """[1.6] A scheme qualifier is stripped, so the answer equals the plain message's answer."""
    plain, qualified = support.call_tool(_args("pacs.008"), _args("pacs.008 (x) UNION SELECT (y)"))
    assert _body(qualified)["legacy_equivalents"] == _body(plain)["legacy_equivalents"]
    assert _body(qualified)["used_by_schemes"] == _body(plain)["used_by_schemes"]


def test_largest_response_within_budget():
    """[3.2] Measured against the message with the most mappings, whatever that is today."""
    [row] = support.db.fetch_all(
        "SELECT iso20022_message_id FROM i22_reference_message_map "
        "WHERE iso20022_message_id IS NOT NULL GROUP BY iso20022_message_id "
        "ORDER BY count(*) DESC LIMIT 1")
    [result] = support.call_tool(_args(row["iso20022_message_id"]))
    size = len(result.content[0].text)
    assert size <= BUDGET_CHARS, f"{row['iso20022_message_id']}: {size:,} characters"


def test_calls_are_fast():
    """[5.3] Each call completes well inside the statement timeout."""
    support.call_tool(_args("MT103"))                       # warm-up: wakes an idle database
    for message, standard, _ in CASES:
        start = time.monotonic()
        support.call_tool(_args(message, standard))
        elapsed = time.monotonic() - start
        assert elapsed < MAX_SECONDS, f"{message}: {elapsed:.2f}s"


def test_role_attributes():
    """[8.3][8.6] Not privileged, read-only by default, has a statement timeout, no write privileges."""
    [role] = support.db.fetch_all(
        "SELECT current_user AS name, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls "
        "FROM pg_roles WHERE rolname = current_user")
    assert role["name"] == "iso20022_mcp"
    assert not (role["rolsuper"] or role["rolcreaterole"] or role["rolcreatedb"] or role["rolbypassrls"])

    member = support.db.fetch_all(
        "SELECT pg_has_role(current_user, oid, 'MEMBER') AS is_member "
        "FROM pg_roles WHERE rolname = 'neon_superuser'")
    assert not (member and member[0]["is_member"]), "role is a member of neon_superuser"

    [s] = support.db.fetch_all(
        "SELECT current_setting('default_transaction_read_only') AS read_only, "
        "(SELECT setting::int FROM pg_settings WHERE name = 'statement_timeout') AS timeout_ms")
    assert s["read_only"] == "on", "default_transaction_read_only is not on"
    assert 0 < s["timeout_ms"] <= MAX_STATEMENT_TIMEOUT_MS, f"statement_timeout is {s['timeout_ms']} ms"

    with pytest.raises(psycopg.Error):
        with support.db.pool.connection() as conn:
            conn.execute("UPDATE i22_release SET release_id = release_id WHERE false")

    # Read-only by default can be switched off in a session; what actually prevents
    # writes is having no write privilege on any table [8.3].
    writable = support.db.fetch_all(
        "SELECT n.nspname || '.' || c.relname AS name FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') "
        "AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%%' "
        "AND (has_table_privilege(c.oid, 'INSERT') OR has_table_privilege(c.oid, 'UPDATE') "
        "OR has_table_privilege(c.oid, 'DELETE') OR has_table_privilege(c.oid, 'TRUNCATE'))")
    assert writable == [], f"role can write to: {[r['name'] for r in writable]}"


def test_access_snapshot_unchanged():
    """[8.5] What the role can read matches the approved snapshot. Only the hash is public."""
    _, digest = support.access_snapshot()
    approved = SNAPSHOT_FILE.read_text().strip() if SNAPSHOT_FILE.exists() else "(none)"
    assert digest == approved, (
        "The tables/columns iso20022_mcp can read have changed (or no snapshot is approved yet). "
        "Run 'python tools/access_snapshot.py' locally, review the list (checklist 8.2), "
        "then save the printed hash to tests/access_snapshot.sha256."
    )
