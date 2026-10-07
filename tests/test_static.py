"""
test_static.py — checks that need no database. They run on every push and pull request.

Release checklist references are in square brackets, e.g. [2.1].
"""
import re

import pytest

import mappings
import shared
import support


@pytest.fixture(scope="module")
def definition():
    return support.tool_definition()


class _NoDatabase:
    """Stands in for fetch_all and records any call, so a swallowed error cannot hide one."""
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        return []


def test_no_select_star():
    """[1.3] Every query names its columns."""
    for path in support.ROOT.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"select\s+(\w+\.)?\*", text, re.IGNORECASE), f"SELECT * in {path.name}"


def test_inputs_typed_and_bounded(definition):
    """[2.1] Every text parameter has a maximum length and a pattern or enum."""
    for name, prop in definition["inputSchema"]["properties"].items():
        variants = prop.get("anyOf", [prop])
        strings = [v for v in variants if v.get("type") == "string"]
        assert strings, f"{name}: no string type"
        for v in strings:
            assert "maxLength" in v, f"{name}: no maxLength"
            assert "pattern" in v or "enum" in v, f"{name}: no pattern or enum"


def test_structured_output_declared(definition):
    """[3.1] The response follows a declared output model."""
    props = definition["outputSchema"]["properties"]
    for field in ("found", "notes", "provenance"):
        assert field in props


def test_name_namespaced(definition):
    """[6.2]"""
    assert definition["name"].startswith("iso20022_")


def test_annotations(definition):
    """[6.3] Read-only, idempotent, closed-world, not destructive."""
    a = definition["annotations"]
    assert a["readOnlyHint"] is True
    assert a["idempotentHint"] is True
    assert a["openWorldHint"] is False
    assert a["destructiveHint"] is False
    assert a["title"], "annotations.title missing (the directory portal flags it)"


@pytest.mark.parametrize("args", [
    {"message": "MT103' UNION SELECT notes FROM i22_reference_message_map --"},
    {"message": "MT103; DROP TABLE i22_release"},
    {"message": "MT103' OR '1'='1"},
    {"message": "%"},
    {"message": "_"},
    {"message": ""},
    {"message": "x" * 41},
    {"message": "MT103", "standard": "SEPA'; --"},
    {"message": "MT103", "standard": "%"},
    {"message": "MT103", "standard": "x" * 31},
], ids=lambda a: repr(a)[:40])
def test_invalid_input_rejected_before_database(monkeypatch, args):
    """[1.6][2.1] Attack strings that break the input pattern never reach the database."""
    spy = _NoDatabase()
    monkeypatch.setattr(shared, "fetch_all", spy)
    [result] = support.call_tool(args)
    assert result.is_error, f"accepted: {args}"
    assert spy.calls == [], "the database was reached"


def test_database_errors_hidden(monkeypatch):
    """[4.1][4.2] Raw database errors never reach the client; the model is told what to do."""
    def broken(*args, **kwargs):
        raise RuntimeError('FATAL: password authentication failed for user "iso20022_mcp" host=ep-secret-host')
    monkeypatch.setattr(shared, "fetch_all", broken)
    [result] = support.call_tool({"message": "MT103"})
    assert result.is_error
    text = result.content[0].text
    for leak in ("iso20022_mcp", "password", "ep-secret-host", "FATAL"):
        assert leak not in text
    assert "Try again" in text


def test_description_steers_scheme_questions(definition):
    """[6.1] Models must not use `standard` to check guessed schemes one by one."""
    assert "call once with only the message" in definition["description"]
    standard = definition["inputSchema"]["properties"]["standard"]
    assert standard["description"].startswith("Omit (or leave empty) unless the user names one specific")


def _fake_db(by_sql):
    """A fetch_all stand-in answering each known query with fixed rows."""
    def fetch(sql, params=()):
        return by_sql.get(sql, [])
    return fetch


def test_iso_response_says_mappings_only(monkeypatch):
    """[6.1] ISO-message responses state the tool has no structure information."""
    monkeypatch.setattr(shared, "fetch_all", _fake_db({}))
    [iso] = support.call_tool({"message": "pacs.008"})
    [legacy] = support.call_tool({"message": "MT103"})
    assert mappings.MAPPINGS_ONLY_NOTE in iso.structured_content["notes"]
    assert mappings.MAPPINGS_ONLY_NOTE not in legacy.structured_content.get("notes", [])


def test_retired_message_note_leads_with_fact(monkeypatch):
    """[7.3] A retired message is described as recognised but retired, not as unknown."""
    monkeypatch.setattr(shared, "fetch_all", _fake_db({
        mappings.SQL_ISO_EXISTS: [{"message_name": "FinancialInvoice", "deactivated_in": "4Q2025"}],
    }))
    [result] = support.call_tool({"message": "tsin.004"})
    notes = result.structured_content["notes"]
    assert any(n.startswith("tsin.004 (FinancialInvoice) is a recognised ISO 20022 message that was retired")
               for n in notes), notes


@pytest.mark.parametrize("standard", ["", "   "])
def test_empty_standard_means_not_given(monkeypatch, standard):
    """Models told to 'leave it empty' may send an empty string; it must work like omitting it."""
    spy = _NoDatabase()
    monkeypatch.setattr(shared, "fetch_all", spy)
    [result] = support.call_tool({"message": "MT103", "standard": standard})
    assert not result.is_error, result.content[0].text
    filtered = [params["std"] for _, params in (c for c in spy.calls if len(c) == 2) if "std" in params]
    assert filtered and all(v is None for v in filtered), filtered


def _iso_row(standard_id, description):
    return {"standard_id": standard_id, "standard_name": standard_id, "ref_id": "pacs.008",
            "ref_name": "Customer Credit Transfer", "iso20022_message_id": "pacs.008",
            "iso20022_message_name": "FIToFICustomerCreditTransfer", "iso20022_deactivated_in": None,
            "status": "ACTIVE", "description": description, "mapping_type": "USES"}


def test_full_scheme_list_is_compact(monkeypatch):
    """[3.2] Unfiltered ISO lists omit descriptions and say how to get one."""
    monkeypatch.setattr(shared, "fetch_all", _fake_db({
        mappings.SQL_BY_ISO: [_iso_row("SEPA", "Long SEPA description."), _iso_row("NPP", "Long NPP description.")],
    }))
    [result] = support.call_tool({"message": "pacs.008"})
    body = result.structured_content
    assert [m["standard_id"] for m in body["used_by_schemes"]] == ["SEPA", "NPP"]
    assert all("description" not in m for m in body["used_by_schemes"])
    assert mappings.COMPACT_LIST_NOTE in body["notes"]


def test_named_scheme_keeps_description(monkeypatch):
    """With standard given, the one scheme's description is returned."""
    monkeypatch.setattr(shared, "fetch_all", _fake_db({
        mappings.SQL_BY_ISO: [_iso_row("SEPA", "Long SEPA description.")],
    }))
    [result] = support.call_tool({"message": "pacs.008", "standard": "SEPA"})
    body = result.structured_content
    assert body["used_by_schemes"][0]["description"] == "Long SEPA description."
    assert mappings.COMPACT_LIST_NOTE not in body.get("notes", [])
