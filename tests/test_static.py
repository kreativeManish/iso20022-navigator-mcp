"""
test_static.py — checks that need no database. They run on every push and pull request.

Release checklist references are in square brackets, e.g. [2.1].
"""
import re

import pytest

import mappings
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
    monkeypatch.setattr(mappings, "fetch_all", spy)
    [result] = support.call_tool(args)
    assert result.is_error, f"accepted: {args}"
    assert spy.calls == [], "the database was reached"


def test_database_errors_hidden(monkeypatch):
    """[4.1][4.2] Raw database errors never reach the client; the model is told what to do."""
    def broken(*args, **kwargs):
        raise RuntimeError('FATAL: password authentication failed for user "iso20022_mcp" host=ep-secret-host')
    monkeypatch.setattr(mappings, "fetch_all", broken)
    [result] = support.call_tool({"message": "MT103"})
    assert result.is_error
    text = result.content[0].text
    for leak in ("iso20022_mcp", "password", "ep-secret-host", "FATAL"):
        assert leak not in text
    assert "Try again" in text
