"""
test_message_details.py — iso20022_message_details without a database.

A small in-memory catalogue stands in for the database, so every input form, the version and variant
notes, the retired note, the not-found design and the output shape are tested on every push and pull
request. Real-data checks are in test_message_details_database.py.

Release checklist references are in square brackets, e.g. [2.1].
"""
import pytest

import message_details as md
import shared
import support

TOOL = support.MESSAGE_TOOL

# message_id -> (version, message_type, name, group, description, deactivated_in)
MESSAGES = {
    "pacs.008": (13, "pacs.008.001.13", "FIToFICustomerCreditTransfer", "Payments Clearing",
                 "Moves a customer credit transfer between banks.", None),
    "pacs.002": (15, "pacs.002.001.15", "FIToFIPaymentStatusReport", "Payments Clearing", "Reports status.", None),
    "pacs.003": (8, "pacs.003.001.08", "FIToFICustomerDirectDebit", "Payments Clearing", "Direct debit.", None),
    "pacs.009": (10, "pacs.009.001.10", "FinancialInstitutionCreditTransfer", "Payments Clearing", "FI transfer.", None),
    "pacs.010": (5, "pacs.010.001.05", "FinancialInstitutionDirectDebit", "Payments Clearing", "FI debit.", None),
    "pacs.004": (14, "pacs.004.001.14", "PaymentReturn", "Payments Clearing", "Return.", None),
    "pacs.007": (13, "pacs.007.001.13", "FIToFIPaymentReversal", "Payments Clearing", "Reversal.", None),
    "camt.053": (14, "camt.053.001.14", "BankToCustomerStatement", "Cash Management", "Account statement.", None),
    "camt.099": (1, "camt.099.001.01", "OldMessage", "Cash Management", "Retired.", "2Q2024"),
    "xxxx.001": (1, "xxxx.001.001.01", "NoStructure", "Other", "No structure.", None),
}
STRUCTURE = [{"depth": 0, "n": 1}, {"depth": 1, "n": 1}, {"depth": 2, "n": 2},
             {"depth": 3, "n": 10}, {"depth": 4, "n": 30}]


class FakeDatabase:
    """Answers the tool's queries from MESSAGES and records every call."""

    def __init__(self, with_structure=True, code_sets=7):
        self.calls = []
        self.with_structure = with_structure
        self.code_sets = code_sets

    def __call__(self, sql, params=()):
        self.calls.append(sql)
        if sql == shared.SQL_RELEASE:
            return [{"release_id": "4Q2025"}]
        if sql == md.SQL_MESSAGE:
            m = MESSAGES.get(params["m"])
            if not m:
                return []
            area = params["m"][:4]
            has_area = area != "xxxx"
            return [{"version": m[0], "message_type": m[1], "message_name": m[2], "message_group": m[3],
                     "rich_description": m[4], "deactivated_in": m[5],
                     "area_code": area if has_area else None,
                     "area_name": {"pacs": "Payments Clearing and Settlement", "camt": "Cash Management"}.get(area),
                     "area_domain": "Payments" if has_area else None}]
        if sql == md.SQL_DEPTHS:
            return STRUCTURE if (self.with_structure and params["m"] != "xxxx.001") else []
        if sql == md.SQL_CODESETS:
            return [{"n": self.code_sets}]
        if sql == md.SQL_CLOSEST:
            ids = sorted((i for i in MESSAGES if i.startswith(params["area"] + ".")),
                         key=lambda i: (abs(int(i.split(".")[1]) - params["n"]), i))
            return [{"message_id": i, "total": len(ids)} for i in ids[:params["limit"]]]
        raise AssertionError("unexpected query")


@pytest.fixture
def db(monkeypatch):
    fake = FakeDatabase()
    monkeypatch.setattr(shared, "fetch_all", fake)
    return fake


def ask(message_id: str) -> dict:
    [result] = support.call_tool({"message_id": message_id}, tool=TOOL)
    assert not result.is_error, result.content[0].text
    return result.structured_content


# ---- input forms [2.3] -------------------------------------------------------------------

@pytest.mark.parametrize("message_id", [
    "pacs.008", "PACS.008", "Pacs.008", "pacs-008", "pacs_008", "pacs 008", "pacs008", " pacs.008 ",
    "pacs.008.001", "pacs.008.001.13",
])
def test_input_forms_are_normalised(db, message_id):
    body = ask(message_id)
    assert body["found"] is True and body["input"] == "pacs.008"
    assert body["latest_version"] == "pacs.008.001.13"


# ---- found response ----------------------------------------------------------------------

def test_found_response_content(db):
    body = ask("pacs.008")
    assert body["name"] == "FIToFICustomerCreditTransfer"
    assert body["navigator_group"] == "Payments Clearing"
    assert body["description"].startswith("Moves a customer")
    assert body["business_area"] == {"code": "pacs", "name": "Payments Clearing and Settlement",
                                     "domain": "Payments"}
    assert body["code_set_count"] == 7
    assert "notes" not in body and "deactivated_in" not in body and "closest_ids" not in body


def test_structure_counts_by_depth(db):
    s = ask("pacs.008")["structure"]
    assert s["total_elements"] == 44             # every depth, wrapper included
    assert s["max_depth"] == 4
    assert s["elements_by_depth"] == {"2": 2, "3": 10, "4": 30}      # starts at depth 2, exact, no cap


def test_zero_code_sets_is_reported_not_omitted(monkeypatch):
    monkeypatch.setattr(shared, "fetch_all", FakeDatabase(code_sets=0))
    assert ask("pacs.008")["code_set_count"] == 0


def test_missing_business_area_does_not_make_the_message_not_found(db):
    body = ask("xxxx.001")
    assert body["found"] is True and "business_area" not in body


def test_no_structure_omits_statistics_and_says_so(db):
    body = ask("xxxx.001")
    assert "structure" not in body
    assert any("No structure is held for xxxx.001" in n for n in body["notes"]), body["notes"]


# ---- version and variant notes -----------------------------------------------------------

def test_matching_full_identifier_has_no_note(db):
    assert "notes" not in ask("pacs.008.001.13")


def test_other_version_gets_a_note(db):
    body = ask("pacs.008.001.08")
    assert body["latest_version"] == "pacs.008.001.13"
    assert any("Version 08 of pacs.008 is not held" in n and "pacs.008.001.13" in n for n in body["notes"])


def test_variant_other_than_one_gets_a_note(db):
    body = ask("pacs.008.002.13")
    assert any("Variant 002 of pacs.008" in n for n in body["notes"]), body["notes"]


def test_short_or_variant_only_form_never_triggers_a_version_note(db):
    assert "notes" not in ask("pacs.008")
    assert "notes" not in ask("pacs.008.001")


# ---- retired messages [7.3] --------------------------------------------------------------

def test_retired_message_is_flagged(db):
    body = ask("camt.099")
    assert body["found"] is True and body["deactivated_in"] == "2Q2024"
    assert any("2Q2024" in n for n in body["notes"]), body["notes"]


# ---- not found [3.5][7.1] ----------------------------------------------------------------

def test_not_found_names_nearest_held_messages(db):
    body = ask("pacs.005")
    assert body["found"] is False and body["input"] == "pacs.005"
    assert body["closest_ids"] == ["pacs.004", "pacs.003", "pacs.007", "pacs.002", "pacs.008"]
    assert len(body["closest_ids"]) == md.MAX_CLOSEST_IDS
    notes = " ".join(body["notes"])
    assert "pacs.005 is outside this dataset" in notes and "Limit the answer to what this response shows" in notes
    assert "Showing 5 of 7 held messages in the pacs business area" in notes
    for field in ("name", "latest_version", "structure", "code_set_count", "description"):
        assert field not in body


def test_not_found_in_an_unknown_area(db):
    body = ask("zzzz.001")
    assert body["found"] is False and "closest_ids" not in body
    assert any("No messages are held for a business area with the code 'zzzz'" in n for n in body["notes"])


# ---- output shape [3.1][3.4][3.6] --------------------------------------------------------

def _no_nulls(value, path="response"):
    if isinstance(value, dict):
        for k, v in value.items():
            assert v is not None, f"{path}.{k} is null"
            _no_nulls(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _no_nulls(v, f"{path}[{i}]")


@pytest.mark.parametrize("message_id", ["pacs.008", "camt.099", "xxxx.001", "pacs.005", "zzzz.001", "pacs.008.001.08"])
def test_no_nulls_and_provenance(db, message_id):
    body = ask(message_id)
    _no_nulls(body)
    assert body["provenance"]["data_baseline"] == "4Q2025"
    assert body["provenance"]["scope"] == md.MESSAGE_SCOPE
    assert isinstance(body["found"], bool)


def test_description_is_labelled_ai_generated_in_the_schema():
    schema = str(support.tool_definition(TOOL)["outputSchema"])
    assert "AI-generated" in schema                                  # [7.2]
    assert "not part of the ISO 20022 standard" in schema.replace("\\n", " ")


def test_counting_rule_travels_with_the_structure_figures(db):
    """[7.1] Responses and schema say what the counts include: optional elements and every alternative of a choice."""
    scope = ask("pacs.008")["provenance"]["scope"]
    assert md.COUNTING_RULE in scope
    for word in ("optional", "alternative", "mandatory", "depths 0 and 1", "omits"):
        assert word in md.COUNTING_RULE.lower()
    schema = str(support.tool_definition(TOOL)["outputSchema"])
    assert schema.count("alternative") >= 2          # total_elements and elements_by_depth


def test_tool_description_points_to_the_mappings_tool():
    text = support.tool_definition(TOOL)["description"]
    assert "iso20022_legacy_and_scheme_mappings" in text and "variant 1" in text


# ---- input safety [1.6][2.1][4.1] --------------------------------------------------------

@pytest.mark.parametrize("message_id", [
    "pacs.008' OR '1'='1", "pacs.008; DROP TABLE i22_release", "pacs.008' UNION SELECT 1 --", "%", "_", "",
    "x" * 25, "1=1", "pacs.008\u202e", "pacs.008\n\x00", "pacs.008.001.13.99", "pacs.0081",
])
def test_pattern_breakers_never_reach_the_database(monkeypatch, message_id):
    fake = FakeDatabase()
    monkeypatch.setattr(shared, "fetch_all", fake)
    [result] = support.call_tool({"message_id": message_id}, tool=TOOL)
    assert result.is_error, f"accepted: {message_id!r}"
    assert fake.calls == [], "the database was reached"


@pytest.mark.parametrize("message_id", ["pac.0080", "pacs.08", "pacs.0O8", "1234.567"])
def test_bad_shapes_are_refused_before_the_database(monkeypatch, message_id):
    fake = FakeDatabase()
    monkeypatch.setattr(shared, "fetch_all", fake)
    [result] = support.call_tool({"message_id": message_id}, tool=TOOL)
    assert result.is_error and fake.calls == []


def test_parse_error_lists_the_accepted_forms():
    with pytest.raises(Exception, match="Accepted forms"):
        md._parse("ab1.234")


def test_database_errors_hidden(monkeypatch):
    """[4.1][4.2] Raw database errors never reach the client; the model is told what to do."""
    def broken(*args, **kwargs):
        raise RuntimeError('FATAL: password authentication failed for user "iso20022_mcp" host=ep-secret-host')
    monkeypatch.setattr(shared, "fetch_all", broken)
    [result] = support.call_tool({"message_id": "pacs.008"}, tool=TOOL)
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
    body = ask("pacs.008")
    assert body["found"] is True and "data_baseline" not in body["provenance"]


def test_log_line_has_the_message_id(monkeypatch, caplog):
    import logging
    monkeypatch.setattr(shared, "fetch_all", FakeDatabase())
    caplog.set_level(logging.INFO, logger="iso20022_mcp.calls")
    support.call_tool({"message_id": "pacs.008"}, tool=TOOL)
    [record] = [r for r in caplog.records if r.name == "iso20022_mcp.calls"]
    assert record.fields["tool"] == TOOL and record.fields["inputs"] == {"message_id": "pacs.008"}
    assert record.fields["outcome"] == "found"
