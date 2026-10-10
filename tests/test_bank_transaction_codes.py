"""
test_bank_transaction_codes.py — iso20022_bank_transaction_codes without a database.

A small in-memory code list stands in for the database, so every input form, the
not-found design and the size ceiling are tested on every push and pull request.
Real-data checks are in test_bank_transaction_codes_database.py.

Release checklist references are in square brackets, e.g. [2.1].
"""
import pytest

import bank_transaction_codes as btc
import shared
import support

TOOL = support.BTC_TOOL

# (domain, family, subfamily). Shaped like the real list: MDOP/IADD exists under one domain only,
# OTHR is both a family and a subfamily, TRAD is both a domain and a subfamily.
COMBINATIONS = [
    ("ACMT", "MCOP", "CHRG"), ("ACMT", "MDOP", "CHRG"), ("ACMT", "OTHR", "OTHR"),
    ("PMNT", "ICDT", "ESCT"), ("PMNT", "ICDT", "OTHR"),
    ("PMNT", "MDOP", "CHRG"), ("PMNT", "MDOP", "IADD"),
    ("PMNT", "RCDT", "ESCT"), ("PMNT", "RCDT", "OTHR"),
    ("TRAD", "MDOP", "CHRG"), ("TRAD", "OTHR", "TRAD"),
]
LEVEL_OF_CONCEPT = {"BankTransactionDomain": "domain", "BankTransactionFamily": "family",
                    "BankTransactionSubFamily": "subfamily"}


def _row(combo):
    d, f, s = combo
    return {"domain": d, "family": f, "subfamily": s,
            "domain_name": f"Domain {d}", "family_name": f"Family {f}", "subfamily_name": f"Subfamily {s}"}


class FakeDatabase:
    """Answers the tool's queries from COMBINATIONS and records every call."""

    def __init__(self, combinations=COMBINATIONS):
        self.combos = combinations
        self.calls = []

    def __call__(self, sql, params=()):
        self.calls.append(sql)
        if sql == shared.SQL_RELEASE:
            return [{"release_id": "4Q2025"}]
        if sql == btc.SQL_BY_SLOTS:
            return [_row(c) for c in self.combos
                    if all(p is None or p == v for p, v in zip((params["d"], params["f"], params["s"]), c, strict=True))]
        if sql == btc.SQL_BY_ANY:
            return [_row(c) for c in self.combos if params["x"] in c]
        if sql == btc.SQL_DESCRIPTION:
            return [{"description": "AI description of " + "-".join((params["d"], params["f"], params["s"]))}]
        if sql == btc.SQL_SEGMENTS:
            concepts = {"BankTransactionDomain": 0, "BankTransactionFamily": 1, "BankTransactionSubFamily": 2}
            return [{"code": code, "concept": concept} for code in params["codes"]
                    for concept, i in concepts.items() if any(c[i] == code for c in self.combos)]
        if sql == btc.SQL_CLOSEST:
            want = (params["d"], params["f"], params["s"])
            scored = [(sum(w is not None and w == v for w, v in zip(want, c, strict=True)), c) for c in self.combos]
            hits = sorted(((-n, c) for n, c in scored if n), key=lambda x: (x[0], x[1]))
            return [{"domain": c[0], "family": c[1], "subfamily": c[2], "total": len(hits)}
                    for _, c in hits[:params["limit"]]]
        raise AssertionError("unexpected query")


@pytest.fixture
def db(monkeypatch):
    fake = FakeDatabase()
    monkeypatch.setattr(shared, "fetch_all", fake)
    return fake


def ask(code: str) -> dict:
    [result] = support.call_tool({"code": code}, tool=TOOL)
    assert not result.is_error, result.content[0].text
    return result.structured_content


def codes(items):
    return [i["code"] for i in items]


# ---- input forms -------------------------------------------------------------------------

@pytest.mark.parametrize("code, canonical", [
    ("PMNT", "PMNT"),
    ("pmnt-rcdt", "PMNT/RCDT"),
    ("PMNT-RCDT-ESCT", "PMNT/RCDT/ESCT"),
    ("PMNT, RCDT, ESCT", "PMNT/RCDT/ESCT"),
    ("PMNT RCDT ESCT", "PMNT/RCDT/ESCT"),
    ("pmnt_rcdt.esct", "PMNT/RCDT/ESCT"),
    ("PMNT/RCDT/ESCT", "PMNT/RCDT/ESCT"),
    ("/rcdt", "/RCDT"),
    ("//esct", "//ESCT"),
    ("/RCDT/ESCT", "/RCDT/ESCT"),
    ("PMNT//ESCT", "PMNT//ESCT"),
    ("PMNT/", "PMNT"),
])
def test_input_forms_are_normalised(db, code, canonical):
    """[2.3] Case and separators are normalised; the response shows the code as interpreted."""
    assert ask(code)["input"] == canonical


def test_domain_lists_its_families_only(db):
    m = ask("PMNT")["matches"][0]
    assert m["level"] == "domain"
    assert m["resolved"] == {"domain": {"code": "PMNT", "name": "Domain PMNT"}}
    assert codes(m["families"]) == ["ICDT", "MDOP", "RCDT"]
    assert "subfamilies" not in m and "domains" not in m
    assert m["counts"] == {"domains": 1, "families": 3, "subfamilies": 4, "combinations": 6}
    assert "all_pairs_exist" not in m


def test_domain_and_family_list_subfamilies(db):
    m = ask("PMNT-RCDT")["matches"][0]
    assert m["level"] == "family"
    assert list(m["resolved"]) == ["domain", "family"]
    assert codes(m["subfamilies"]) == ["ESCT", "OTHR"]
    assert "families" not in m


def test_full_path_gives_description_without_lists(db):
    m = ask("PMNT-RCDT-ESCT")["matches"][0]
    assert m["level"] == "combination"
    assert list(m["resolved"]) == ["domain", "family", "subfamily"]
    assert m["description"] == "AI description of PMNT-RCDT-ESCT"
    assert not {"domains", "families", "subfamilies", "counts"} & set(m)


def test_family_alone_lists_domains_and_subfamilies(db):
    m = ask("/RCDT")["matches"][0]
    assert m["level"] == "family"
    assert codes(m["domains"]) == ["PMNT"] and codes(m["subfamilies"]) == ["ESCT", "OTHR"]
    assert m["all_pairs_exist"] is True


def test_subfamily_alone_lists_domains_and_families(db):
    m = ask("//ESCT")["matches"][0]
    assert m["level"] == "subfamily"
    assert codes(m["domains"]) == ["PMNT"] and codes(m["families"]) == ["ICDT", "RCDT"]


def test_family_and_subfamily_list_domains(db):
    m = ask("/MDOP/CHRG")["matches"][0]
    assert codes(m["domains"]) == ["ACMT", "PMNT", "TRAD"]
    assert "families" not in m and "subfamilies" not in m


def test_domain_and_subfamily_list_families(db):
    m = ask("PMNT//ESCT")["matches"][0]
    assert codes(m["families"]) == ["ICDT", "RCDT"]
    assert "domains" not in m and "subfamilies" not in m


def test_unpaired_lists_are_flagged_with_the_pair_count(db):
    body = ask("/MDOP")
    m = body["matches"][0]
    assert codes(m["domains"]) == ["ACMT", "PMNT", "TRAD"] and codes(m["subfamilies"]) == ["CHRG", "IADD"]
    assert m["all_pairs_exist"] is False
    assert any("only 4 of 6 possible domain-subfamily pairs exist" in n for n in body["notes"]), body["notes"]


def test_single_combination_includes_description(db):
    """A code that resolves to one path (here IADD) carries that path's description."""
    m = ask("IADD")["matches"][0]
    assert m["level"] == "subfamily" and m["description"] == "AI description of PMNT-MDOP-IADD"


# ---- ambiguous bare codes ----------------------------------------------------------------

def test_bare_code_is_matched_at_every_level(db):
    body = ask("OTHR")
    assert [m["level"] for m in body["matches"]] == ["family", "subfamily"]
    assert any("'/OTHR' for a family" in n and "'//OTHR' for a subfamily" in n for n in body["notes"])


def test_domain_and_subfamily_code(db):
    body = ask("TRAD")
    assert [m["level"] for m in body["matches"]] == ["domain", "subfamily"]


def test_leading_slash_pins_the_level(db):
    body = ask("/OTHR")
    assert [m["level"] for m in body["matches"]] == ["family"]
    assert not any("exists at more than one level" in n for n in body.get("notes", []))


# ---- not found ---------------------------------------------------------------------------

def test_known_parts_without_a_combination(db):
    body = ask("PMNT-RCDT-IADD")
    assert body["found"] is False and body["matches"] == []
    assert body["segments"] == [{"code": "PMNT", "exists_as": ["domain"]},
                                {"code": "RCDT", "exists_as": ["family"]},
                                {"code": "IADD", "exists_as": ["subfamily"]}]
    # two parts shared first (ordered by code), then one part shared
    assert body["closest_paths"][:3] == ["PMNT-MDOP-IADD", "PMNT-RCDT-ESCT", "PMNT-RCDT-OTHR"]
    assert len(body["closest_paths"]) == 5
    notes = " ".join(body["notes"])
    assert "not a combination in the ISO external code list" in notes
    assert "Showing 5 of" in notes
    assert "bank-specific" in notes and "Do not infer" in notes


def test_unknown_code_is_not_in_the_list(db):
    body = ask("ZZZZ")
    assert body["found"] is False
    assert body["segments"] == [{"code": "ZZZZ"}]          # exists_as omitted when it exists nowhere
    assert "closest_paths" not in body
    assert "ZZZZ is not in the ISO external code list." in body["notes"]
    assert not any("invalid" in n.lower() for n in body["notes"])


def test_code_at_the_wrong_level_says_where_it_exists(db):
    body = ask("PMNT//RCDT")
    assert body["found"] is False
    assert "RCDT is not a subfamily code; it exists as a family." in body["notes"]


# ---- result size [3.2] -------------------------------------------------------------------

def test_ceiling_limits_listed_items_and_says_so(db, monkeypatch):
    monkeypatch.setattr(btc, "MAX_LISTED_ITEMS", 3)
    body = ask("/MDOP")
    m = body["matches"][0]
    assert len(m["domains"]) + len(m.get("subfamilies", [])) == 3
    assert codes(m["domains"]) == ["ACMT", "PMNT", "TRAD"]          # ordered by code
    assert m["counts"]["combinations"] == 4                         # counts cover everything, not the list
    assert any(n.startswith("Showing 3 of 5 listed items") for n in body["notes"]), body["notes"]


def test_ceiling_is_shared_across_matches(db, monkeypatch):
    monkeypatch.setattr(btc, "MAX_LISTED_ITEMS", 4)
    body = ask("OTHR")
    listed = sum(len(m.get(k, [])) for m in body["matches"] for k in ("domains", "families", "subfamilies"))
    assert listed == 4


# ---- output shape [3.1][3.4][3.6] --------------------------------------------------------

def _no_nulls(value, path="response"):
    if isinstance(value, dict):
        for k, v in value.items():
            assert v is not None, f"{path}.{k} is null"
            _no_nulls(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _no_nulls(v, f"{path}[{i}]")


@pytest.mark.parametrize("code", ["PMNT", "PMNT-RCDT-ESCT", "/MDOP", "OTHR", "ZZZZ", "PMNT-RCDT-IADD"])
def test_no_nulls_and_provenance(db, code):
    body = ask(code)
    _no_nulls(body)
    assert body["provenance"]["data_baseline"] == "4Q2025"
    assert body["provenance"]["scope"] == btc.BTC_SCOPE
    assert isinstance(body["found"], bool)


def test_matches_present_even_when_empty(db):
    assert ask("ZZZZ")["matches"] == []


def test_fields_left_out_by_design_are_not_in_the_schema():
    """Design decisions 2026-10-07: no deactivation, no short description, no message list."""
    schema = str(support.tool_definition(TOOL)["outputSchema"])
    for field in ("deactivated", "short_description", "message"):
        assert field not in schema, field


def test_description_lists_every_input_form():
    text = support.tool_definition(TOOL)["description"]
    for form in ("- PMNT (", "- PMNT-RCDT (", "- PMNT-RCDT-ESCT (", "- /RCDT (", "- //ESCT (",
                 "- /RCDT/ESCT (", "- PMNT//ESCT (", "- ESCT ("):
        assert form in text, form
    assert "browse" in text and "bank-specific" in text


# ---- input safety [1.6][2.1][4.1] --------------------------------------------------------

@pytest.mark.parametrize("code", [
    "PMNT' OR '1'='1", "PMNT; DROP TABLE i22_release", "PMNT' UNION SELECT 1 --", "%", "_", "",
    "x" * 31, "1=1", "PMNT‮RCDT", "PMNT\nRCDT\x00",
])
def test_pattern_breakers_never_reach_the_database(monkeypatch, code):
    fake = FakeDatabase()
    monkeypatch.setattr(shared, "fetch_all", fake)
    [result] = support.call_tool({"code": code}, tool=TOOL)
    assert result.is_error, f"accepted: {code!r}"
    assert fake.calls == [], "the database was reached"


@pytest.mark.parametrize("code", [
    "///", "/", "//", "PMNT-RCDT-ESCT-OTHR", "PMN", "PMNTX", "PMNT--ESCT", "PMNT-", "-PMNT", "PMNT//ESCT/OTHR",
    "PMNT-RCD/ESCT", "PM NT",
])
def test_bad_shapes_list_the_accepted_forms(monkeypatch, code):
    """Input that passes the pattern but is not a code shape gets a message the model can act on."""
    fake = FakeDatabase()
    monkeypatch.setattr(shared, "fetch_all", fake)
    [result] = support.call_tool({"code": code}, tool=TOOL)
    assert result.is_error
    assert "Accepted forms" in result.content[0].text
    assert fake.calls == []


def test_database_errors_hidden(monkeypatch):
    """[4.1][4.2] Raw database errors never reach the client; the model is told what to do."""
    def broken(*args, **kwargs):
        raise RuntimeError('FATAL: password authentication failed for user "iso20022_mcp" host=ep-secret-host')
    monkeypatch.setattr(shared, "fetch_all", broken)
    [result] = support.call_tool({"code": "PMNT"}, tool=TOOL)
    assert result.is_error
    text = result.content[0].text
    for leak in ("iso20022_mcp", "password", "ep-secret-host", "FATAL"):
        assert leak not in text
    assert "Try again" in text


def test_provenance_failure_does_not_fail_the_answer(monkeypatch):
    """[3.6] If the release lookup fails the answer is still returned, without data_baseline."""
    fake = FakeDatabase()

    def flaky(sql, params=()):
        if sql == shared.SQL_RELEASE:
            raise RuntimeError("boom")
        return fake(sql, params)
    monkeypatch.setattr(shared, "fetch_all", flaky)
    body = ask("PMNT")
    assert body["found"] is True and "data_baseline" not in body["provenance"]
