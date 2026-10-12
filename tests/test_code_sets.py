"""
test_code_sets.py — iso20022_code_sets without a database.

A small in-memory dataset stands in for the database, so every input kind, the paging
rules, the size budget and the not-found design are tested on every push and pull request.
Real-data checks are in test_code_sets_database.py.

Release checklist references are in square brackets, e.g. [2.1].
"""
import logging

import pytest

import code_sets
import mappings
import shared
import support

TOOL = support.CODE_SETS_TOOL
BUDGET_CHARS = 20_000          # [3.2] the same budget as the database tests (raised from 16,000 on 2026-10-12)
MIN_ROWS_NON_FINAL = 10        # a non-final page with fewer rows means the budget has collapsed


def _desc(words: int) -> str:
    return " ".join(["word"] * words)


def _values(prefix: str, count: int, words=lambda i: 5 + i % 45) -> list[dict]:
    return [{"code": f"{prefix}{i:03d}", "code_name": f"Name {prefix}{i:03d}", "description": _desc(words(i)),
             "deactivated_in": None} for i in range(count)]


def _set(name: str, group="Payment Processing", description="Reason codes.", deactivated_in=None) -> dict:
    return {"codeset_name": f"External{name}1Code", "group_name": group, "description": description,
            "deactivated_in": deactivated_in}


def dataset() -> dict:
    balance = [{"code": "CLBD", "code_name": "ClosingBooked", "description": "Balance at the end of the period.",
                "deactivated_in": None},
               {"code": "OPBD", "code_name": "OpeningBooked", "description": "Balance at the start.",
                "deactivated_in": None},
               {"code": "OTHR", "code_name": "Other", "description": "Other balance.", "deactivated_in": None}]
    odd = [{"code": "A1", "code_name": "Odd", "description": "line one\nline two\t| pipe    spaces",
            "deactivated_in": None}]
    returns = [{"code": "AC01", "code_name": "IncorrectAccountNumber", "description": "Account number is invalid.",
                "deactivated_in": None},
               {"code": "OTHR", "code_name": "Other", "description": "Other reason.", "deactivated_in": None},
               {"code": "ZZ99", "code_name": "Obsolete", "description": "No longer used.", "deactivated_in": "4Q2025"}]
    return {
        "sets": {
            "BalanceType": _set("BalanceType", group="Account & Balance", description="Balance types."),
            "Purpose": _set("Purpose"),
            "ReceivedReason": _set("ReceivedReason"),
            "ReturnReason": _set("ReturnReason"),
            "AccountIdentification": _set("AccountIdentification", group="Party & Identity"),
            "Squeezed": _set("Squeezed"),
            "Odd": _set("Odd"),
            "NoDescription": {"codeset_name": None, "group_name": None, "description": None,
                              "deactivated_in": None},
            "BankTransactionFamily": _set("BankTransactionFamily", group="Clearing & Settlement"),
        },
        "values": {
            "BalanceType": balance,
            "Purpose": _values("P", 332),
            "ReceivedReason": _values("R", 1),
            "ReturnReason": returns,
            "AccountIdentification": _values("A", 4),
            "Squeezed": _values("S", 60, words=lambda i: 40),
            "Odd": odd,
            "NoDescription": _values("N", 2),
            "BankTransactionFamily": [{"code": "OTHR", "code_name": "Other", "description": "BTC other.",
                                       "deactivated_in": None}],
        },
        "set_messages": {
            "BalanceType": ["camt.053", "camt.052", "camt.053"],      # duplicates across variants collapse
            "Purpose": ["pain.001", "pacs.008"],
            "ReturnReason": ["pacs.004"],
            "AccountIdentification": [f"msg{i:03d}.001" for i in range(193)],
            "Squeezed": [f"msg{i:03d}.001" for i in range(800)],
        },
        "message_sets": {
            "camt.053": ["BalanceType", "BankTransactionFamily"],
            "fxtr.014": [],
            "tsin.004": [],
            "pacs.008": [],
        },
        "message_info": {
            "camt.053": ("BankToCustomerStatement", None),
            "fxtr.014": ("ForeignExchangeTradeInstruction", None),
            "tsin.004": ("FinancialInvoice", "4Q2025"),
            "pacs.008": ("FIToFICustomerCreditTransfer", None),
        },
    }


class FakeDatabase:
    """Answers the tool's queries from the dataset and records every call."""

    def __init__(self, data=None):
        self.d = data or dataset()
        self.calls = []

    def _find_set(self, name: str, btc: bool):
        s = name.lower()
        for concept, attrs in self.d["sets"].items():
            if (concept.lower() in code_sets._BTC_LOWER) == btc and s in (concept.lower(),
                                                                          (attrs["codeset_name"] or "").lower()):
                return concept, attrs
        return None

    def _active(self, concept: str) -> int:
        return sum(1 for v in self.d["values"][concept] if v["deactivated_in"] is None)

    def __call__(self, sql, params=()):
        self.calls.append(sql)
        if sql == shared.SQL_RELEASE:
            return [{"release_id": "4Q2025"}]
        if sql == code_sets.SQL_CODE_SET:
            hit = self._find_set(params["s"], btc=False)
            return [{"concept": hit[0], **hit[1]}] if hit else []
        if sql == code_sets.SQL_BTC_CONCEPT:
            hit = self._find_set(params["s"], btc=True)
            return [{"concept": hit[0]}] if hit else []
        if sql == code_sets.SQL_VALUE_COUNTS:
            vals = self.d["values"][params["c"]]
            return [{"active": self._active(params["c"]), "deactivated": len(vals) - self._active(params["c"])}]
        if sql == code_sets.SQL_VALUES:
            want = params["code"].lower() if params["code"] else None
            return sorted((v for v in self.d["values"][params["c"]] if want is None or v["code"].lower() == want),
                          key=lambda v: v["code"])
        if sql == code_sets.SQL_SET_MESSAGES:
            return [{"message_id": m} for m in sorted(set(self.d["set_messages"].get(params["c"], [])))]
        if sql == code_sets.SQL_MESSAGE_SETS:
            return [{"concept": c, "navigator_group": self.d["sets"][c]["group_name"], "code_count": self._active(c)}
                    for c in sorted(self.d["message_sets"].get(params["iso"], []))]
        if sql == mappings.SQL_ISO_EXISTS:
            info = self.d["message_info"].get(params["iso"])
            return [{"message_name": info[0], "deactivated_in": info[1]}] if info else []
        if sql == code_sets.SQL_CODE_MATCHES:
            want = params["code"].lower()
            return [{"concept": c, **v} for c in sorted(self.d["values"]) if c.lower() not in code_sets._BTC_LOWER
                    for v in self.d["values"][c] if v["code"].lower() == want]
        raise AssertionError("unexpected query")


@pytest.fixture
def db(monkeypatch):
    fake = FakeDatabase()
    monkeypatch.setattr(shared, "fetch_all", fake)
    return fake


def call(args: dict):
    [result] = support.call_tool(args, tool=TOOL)
    assert not result.is_error, result.content[0].text
    return result


def ask(message: str, **extra) -> dict:
    return call({"message": message, **extra}).structured_content


def rows_of(text: str) -> tuple[str, list[str]]:
    header, *rows = text.split("\n")
    return header, rows


def walk(message: str, field: str, **extra) -> list[dict]:
    """Every page of a list, following next_offset."""
    pages, offset = [], 0
    while True:
        result = call({"message": message, **extra, **({"offset": offset} if offset else {})})
        assert len(result.content[0].text) <= BUDGET_CHARS, f"page at offset {offset}: over budget"
        body = result.structured_content
        pages.append(body)
        page = body["page"]
        assert page["offset"] == offset
        if page["complete"]:
            assert "next_offset" not in page
            return pages
        assert page["next_offset"] > offset, "paging did not advance"
        offset = page["next_offset"]
        assert len(pages) < 1000, "paging does not end"


# ---- code set input ------------------------------------------------------------------------

@pytest.mark.parametrize("message", ["BalanceType", "balancetype", "  BALANCETYPE ", "ExternalBalanceType1Code"])
def test_code_set_found_by_concept_or_full_name(db, message):
    """[2.3] Case, whitespace and the full ISO name all reach the same code set."""
    body = ask(message)
    assert body["found"] is True and body["input_kind"] == "code_set" and body["input"] == "BalanceType"
    assert body["code_set"] == {"concept": "BalanceType", "name": "ExternalBalanceType1Code",
                                "navigator_group": "Account & Balance", "concept_description": "Balance types.",
                                "active_values": 3}


def test_values_are_pipe_delimited_with_header(db):
    body = ask("BalanceType")
    header, rows = rows_of(body["values"])
    assert header == "code|name|description|deactivated_in"
    assert rows[0] == "CLBD|ClosingBooked|Balance at the end of the period.|"
    assert [r.split("|")[0] for r in rows] == ["CLBD", "OPBD", "OTHR"]
    assert body["page"] == {"offset": 0, "returned": 3, "total": 3, "complete": True}


def test_notes_say_ai_generated_and_navigator_group(db):
    notes = ask("BalanceType")["notes"]
    assert "concept_description is AI-generated." in notes
    assert code_sets.GROUP_NOTE in notes


def test_missing_description_and_group_drop_their_notes(db):
    body = ask("NoDescription")
    assert "concept_description" not in body["code_set"] and "navigator_group" not in body["code_set"]
    assert code_sets.AI_NOTE not in body["notes"] and code_sets.GROUP_NOTE not in body["notes"]


def test_used_in_messages_lists_ids_once(db):
    body = ask("BalanceType")
    assert body["used_in_messages"] == ["camt.052", "camt.053"]


def test_set_without_messages_says_so(db):
    body = ask("ReceivedReason")
    assert "used_in_messages" not in body
    assert any(n.startswith("No messages are recorded as using ReceivedReason") for n in body["notes"])


def test_one_value_set_is_one_complete_row(db):
    body = ask("ReceivedReason")
    _, rows = rows_of(body["values"])
    assert len(rows) == 1
    assert body["page"] == {"offset": 0, "returned": 1, "total": 1, "complete": True}


def test_deactivated_value_is_flagged_and_noted(db):
    body = ask("ReturnReason")
    _, rows = rows_of(body["values"])
    assert "ZZ99|Obsolete|No longer used.|4Q2025" in rows
    assert body["code_set"]["active_values"] == 2 and body["code_set"]["deactivated_values"] == 1
    assert any(n.startswith("This page includes deactivated values") for n in body["notes"])


def test_no_deactivated_note_when_none_on_the_page(db):
    body = ask("BalanceType")
    assert "deactivated_values" not in body["code_set"]
    assert not any("deactivated" in n for n in body["notes"])


def test_deactivated_code_set_is_flagged(monkeypatch):
    data = dataset()
    data["sets"]["BalanceType"]["deactivated_in"] = "4Q2025"
    monkeypatch.setattr(shared, "fetch_all", FakeDatabase(data))
    body = ask("BalanceType")
    assert body["found"] is True and body["code_set"]["deactivated_in"] == "4Q2025"
    assert any("marked deactivated in the Navigator's 4Q2025" in n for n in body["notes"])


def test_values_cannot_break_the_row_layout(db):
    """Pipes, tabs, newlines and runs of spaces inside a value become single spaces."""
    body = ask("Odd")
    _, rows = rows_of(body["values"])
    assert rows == ["A1|Odd|line one line two pipe spaces|"]
    assert all(len(r.split("|")) == 4 for r in rows)


# ---- the code parameter --------------------------------------------------------------------

def test_code_checks_one_value_case_insensitively(db):
    body = ask("BalanceType", code="clbd")
    _, rows = rows_of(body["values"])
    assert body["found"] is True and rows == ["CLBD|ClosingBooked|Balance at the end of the period.|"]
    assert any("Only the value matching 'clbd'" in n for n in body["notes"])


def test_code_not_in_set_is_an_explained_not_found(db):
    body = ask("BalanceType", code="NOPE")
    assert body["found"] is False and body["code_set"]["concept"] == "BalanceType"
    assert "values" not in body and "used_in_messages" not in body
    assert any("'NOPE' is not a value of BalanceType" in n and "Do not infer" in n for n in body["notes"])


def test_deactivated_code_is_returned_flagged_not_hidden(db):
    body = ask("ReturnReason", code="ZZ99")
    assert body["found"] is True
    assert rows_of(body["values"])[1] == ["ZZ99|Obsolete|No longer used.|4Q2025"]


@pytest.mark.parametrize("message", ["camt.053", "OTHR", "NoSuchThing"])
def test_code_is_ignored_with_a_note_outside_code_sets(db, message):
    body = ask(message, code="CLBD")
    assert code_sets._code_ignored_note() in body["notes"]


def test_empty_code_means_not_given(db):
    body = ask("BalanceType", code="")
    assert len(rows_of(body["values"])[1]) == 3


# ---- paging [3.2] --------------------------------------------------------------------------

def test_large_set_pages_by_whole_rows(db):
    """Walk every page of a 332-value set: no row cut, lost or repeated, each page within budget."""
    pages = walk("Purpose", "values")
    expected = [code_sets._row(v["code"], v["code_name"], v["description"], v["deactivated_in"])
                for v in db.d["values"]["Purpose"]]
    got = []
    for i, body in enumerate(pages):
        header, rows = rows_of(body["values"])
        assert header == code_sets.HEADER_VALUES, "every page repeats the header"
        assert all(len(r.split("|")) == 4 for r in rows), "a row was cut"
        assert body["page"]["returned"] == len(rows) and body["page"]["total"] == 332
        if i < len(pages) - 1:
            assert len(rows) >= MIN_ROWS_NON_FINAL, f"page {i + 1} has only {len(rows)} rows"
        got += rows
    assert len(pages) > 1
    assert got == expected


def test_every_page_carries_the_set_context(db):
    pages = walk("Purpose", "values")
    for body in pages:
        assert body["code_set"]["concept"] == "Purpose"
        assert body["used_in_messages"] == ["pacs.008", "pain.001"]
        assert code_sets.AI_NOTE in body["notes"]


def test_paging_notes_say_how_to_continue(db):
    pages = walk("Purpose", "values")
    first, last = pages[0], pages[-1]
    nxt = first["page"]["next_offset"]
    assert any("not the complete list" in n and f"offset={nxt}" in n for n in first["notes"]), first["notes"]
    assert any("this is the final page" in n for n in last["notes"]), last["notes"]
    assert last["page"]["complete"] is True


def test_offset_past_the_end_is_explained(db):
    body = ask("BalanceType", offset=50)
    assert body["page"] == {"offset": 50, "returned": 0, "total": 3, "complete": True}
    assert rows_of(body["values"])[1] == []
    assert any("past the end" in n for n in body["notes"])


def test_long_message_list_fits_with_the_values(db):
    """A small set used by many messages: the whole message list is sent and the page stays in budget."""
    result = call({"message": "AccountIdentification"})
    body = result.structured_content
    assert len(body["used_in_messages"]) == 193
    assert len(result.content[0].text) <= BUDGET_CHARS
    assert body["page"]["complete"] is True


def test_huge_message_list_shrinks_the_pages_but_paging_still_advances(db):
    """The message list takes budget from the values: pages get smaller, never empty, and the walk ends."""
    pages = walk("Squeezed", "values")
    total = sum(b["page"]["returned"] for b in pages)
    assert total == 60 and all(b["page"]["returned"] >= 1 for b in pages)
    assert len(pages) > 1


def test_budget_is_measured_not_assumed(db):
    """The same set pages into more, smaller pages when the envelope is bigger."""
    small = walk("Purpose", "values")
    big_data = dataset()
    big_data["set_messages"]["Purpose"] = [f"msg{i:03d}.001" for i in range(600)]
    db.d = big_data
    big = walk("Purpose", "values")
    assert len(big) > len(small)


# ---- message input -------------------------------------------------------------------------

def test_message_lists_code_sets_with_counts(db):
    body = ask("camt.053")
    assert body["found"] is True and body["input_kind"] == "message" and body["input"] == "camt.053"
    assert body["iso20022_message_name"] == "BankToCustomerStatement"
    header, rows = rows_of(body["code_sets"])
    assert header == "concept|navigator_group|code_count"
    assert rows == ["BalanceType|Account & Balance|3", "BankTransactionFamily|Clearing & Settlement|1"]
    assert body["page"]["complete"] is True


def test_message_notes_cover_btc_group_and_scope(db):
    notes = " ".join(ask("camt.053")["notes"])
    assert "iso20022_bank_transaction_codes" in notes
    assert code_sets.GROUP_NOTE in notes
    assert "variant 1" in notes and "not element level" in notes


@pytest.mark.parametrize("message", ["CAMT.053", "camt.053.001.08", "camt.053 (CORE)"])
def test_message_forms_are_normalised(db, message):
    body = ask(message)
    assert body["input"] == "camt.053"
    if message != "CAMT.053":
        assert any("not per version or variant" in n for n in body["notes"])


def test_message_without_code_sets_is_an_explained_not_found(db):
    body = ask("pacs.008")
    assert body["found"] is False
    assert any("no code sets are recorded for it" in n for n in body["notes"])
    assert any("Do not infer" in n for n in body["notes"])


def test_unknown_message_is_not_in_the_dataset(db):
    body = ask("abcd.999")
    assert body["found"] is False
    assert "abcd.999 is not an ISO 20022 message in this dataset." in body["notes"]


def test_retired_message_is_flagged(db):
    body = ask("tsin.004")
    assert body["found"] is False and body["iso20022_deactivated_in"] == "4Q2025"
    assert any(n.startswith("tsin.004 (FinancialInvoice) is a recognised ISO 20022 message that was retired")
               for n in body["notes"]), body["notes"]


def test_forex_message_notes_the_known_gap(db):
    assert code_sets.FOREX_GAP in ask("fxtr.014")["notes"]


# ---- code input ----------------------------------------------------------------------------

def test_code_in_several_sets_returns_each_and_skips_btc(db):
    body = ask("othr")
    assert body["found"] is True and body["input_kind"] == "code" and body["input"] == "othr"
    header, rows = rows_of(body["matches"])
    assert header == "concept|code|name|description|deactivated_in"
    assert [r.split("|")[0] for r in rows] == ["BalanceType", "ReturnReason"]     # BTC family OTHR excluded
    assert any("exists in 2 code sets" in n for n in body["notes"])
    assert any("iso20022_bank_transaction_codes" in n for n in body["notes"])


def test_single_match_has_no_ambiguity_note(db):
    body = ask("CLBD")
    assert rows_of(body["matches"])[1] == ["BalanceType|CLBD|ClosingBooked|Balance at the end of the period.|"]
    assert not any("exists in" in n for n in body["notes"])


def test_code_match_flags_deactivated(db):
    body = ask("ZZ99")
    assert rows_of(body["matches"])[1] == ["ReturnReason|ZZ99|Obsolete|No longer used.|4Q2025"]
    assert any(n.startswith("This page includes deactivated matches") for n in body["notes"])


# ---- not found [3.5] -----------------------------------------------------------------------

def test_btc_concept_points_to_the_btc_tool(db):
    for name in ("BankTransactionFamily", "ExternalBankTransactionFamily1Code"):
        body = ask(name)
        assert body["found"] is False and body["input_kind"] == "unknown"
        assert any("iso20022_bank_transaction_codes" in n for n in body["notes"]), body["notes"]
        assert "code_set" not in body and "values" not in body


def test_unknown_input_is_explained_and_redirects(db):
    body = ask("NoSuchThing")
    assert body["found"] is False and body["input_kind"] == "unknown"
    notes = " ".join(body["notes"])
    assert "Do not infer" in notes
    assert "iso20022_bank_transaction_codes" in notes and "iso20022_legacy_and_scheme_mappings" in notes
    assert code_sets.FOREX_GAP in body["notes"]


# ---- output shape [3.1][3.4][3.6] ----------------------------------------------------------

def _no_nulls(value, path="response"):
    if isinstance(value, dict):
        for k, v in value.items():
            assert v is not None, f"{path}.{k} is null"
            _no_nulls(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _no_nulls(v, f"{path}[{i}]")


@pytest.mark.parametrize("message", ["BalanceType", "camt.053", "OTHR", "NoSuchThing", "pacs.008", "NoDescription"])
def test_no_nulls_and_provenance(db, message):
    body = ask(message)
    _no_nulls(body)
    assert body["provenance"]["data_baseline"] == "4Q2025"
    assert body["provenance"]["scope"] == code_sets.CODE_SETS_SCOPE
    assert isinstance(body["found"], bool)


def test_description_points_to_the_other_tools():
    """[6.1] When not to use it, and where to go instead."""
    text = support.tool_definition(TOOL)["description"]
    assert "iso20022_bank_transaction_codes" in text and "iso20022_legacy_and_scheme_mappings" in text
    assert "page.complete" in text and "next_offset" in text
    assert "AI-generated" in text


def test_server_instructions_mention_code_sets_and_paging():
    text = support.server.INSTRUCTIONS
    assert "code set" in text and "next_offset" in text


# ---- input safety [1.6][2.1][4.1] ----------------------------------------------------------

@pytest.mark.parametrize("args", [
    {"message": "BalanceType' OR '1'='1"},
    {"message": "BalanceType; DROP TABLE i22_codeset"},
    {"message": "%"},
    {"message": "_x%"},
    {"message": ""},
    {"message": "x" * 61},
    {"message": "BalanceType", "code": "CLBD'; --"},
    {"message": "BalanceType", "code": "%"},
    {"message": "BalanceType", "code": "x" * 11},
    {"message": "BalanceType", "offset": -1},
    {"message": "BalanceType", "offset": 100_001},
    {"message": "BalanceType", "offset": "abc"},
], ids=lambda a: repr(a)[:40])
def test_invalid_input_rejected_before_database(monkeypatch, args):
    """[1.6][2.1] Attack strings and bad numbers never reach the database."""
    fake = FakeDatabase()
    monkeypatch.setattr(shared, "fetch_all", fake)
    [result] = support.call_tool(args, tool=TOOL)
    assert result.is_error, f"accepted: {args}"
    assert fake.calls == [], "the database was reached"


def test_database_errors_hidden(monkeypatch):
    """[4.1][4.2] Raw database errors never reach the client; the model is told what to do."""
    def broken(*args, **kwargs):
        raise RuntimeError('FATAL: password authentication failed for user "iso20022_mcp" host=ep-secret-host')
    monkeypatch.setattr(shared, "fetch_all", broken)
    [result] = support.call_tool({"message": "BalanceType"}, tool=TOOL)
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
    shared._release_cache = None
    body = ask("BalanceType")
    assert body["found"] is True and "data_baseline" not in body["provenance"]


def test_log_line_declares_message_and_code(db, caplog):
    caplog.set_level(logging.INFO, logger="iso20022_mcp.calls")
    ask("BalanceType", code="CLBD")
    [record] = [r for r in caplog.records if r.name == "iso20022_mcp.calls"]
    f = record.fields
    assert f["tool"] == TOOL and f["outcome"] == "found"
    assert f["inputs"]["message"] == "BalanceType" and f["inputs"]["code"] == "CLBD"
