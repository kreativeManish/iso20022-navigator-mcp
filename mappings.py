"""
mappings.py — the iso20022_find_mappings tool.

Answers two kinds of question from i22_reference_message_map:
  - legacy -> ISO:  "What is the ISO 20022 equivalent of MT103?"   (mapping_type EQUIVALENT)
  - ISO -> world:   "Which legacy messages map to pacs.008, and which
                     payment schemes use it?"                        (EQUIVALENT + USES)

Release checklist references are in square brackets, e.g. [1.2].
"""
import json
import logging
import re
import time
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from db import fetch_all

log = logging.getLogger("iso20022_mcp.mappings")

# --------------------------------------------------------------------------
# Input recognition
# --------------------------------------------------------------------------
# ISO 20022 message: 'pacs.008', optionally with version 'pacs.008.001.08'
# and/or a scheme qualifier 'pacs.009 (CORE)'. Mappings are message-level,
# so only the 'pacs.008' part is used.
ISO_ID = re.compile(r"^([a-z]{4}\.\d{3})(\.\d{3}\.\d{2})?(\s*\(.*\))?$", re.IGNORECASE)
# SWIFT MT written with a prefix: 'MT103', 'MT 202COV'
MT_PREFIXED = re.compile(r"^MT\s*(\d{3}\w*)$", re.IGNORECASE)
# SWIFT MT written as a bare number: '103', '202COV'
MT_BARE = re.compile(r"^\d{3}\w*$")


# --------------------------------------------------------------------------
# Output models [3.1] — only fields declared here can leave the server.
# direction and notes are deliberately absent (see i22 decisions.docx).
# --------------------------------------------------------------------------
class Mapping(BaseModel):
    standard_id: str = Field(description="Identifier of the payment standard or scheme, e.g. SWIFT_MT, SEPA")
    standard_name: str
    ref_id: str = Field(description="Message identifier as the standard writes it, e.g. 103, MT103, pacs.008")
    ref_name: str
    iso20022_message_id: str | None = Field(None, description="ISO 20022 message; absent when there is no equivalent")
    iso20022_message_name: str | None = None
    iso20022_deactivated_in: str | None = Field(
        None, description="Navigator data load (quarter) in which this ISO 20022 message was marked "
                          "deactivated; absent if current")
    status: Literal["ACTIVE", "DEPRECATED"]
    description: str | None = Field(None, description="Explanation of this specific mapping")


class Provenance(BaseModel):
    source: str = "ISO 20022 Navigator reference data (https://www.isonavigator.io/iso20022/)"
    scope: str = "Message-level mappings only (not field-level). ISO 20022 messages at their latest version only."
    data_baseline: str | None = Field(
        None, description="Quarter of the Navigator's baseline data load, e.g. 4Q2025: data reflects that load "
                          "unless stated otherwise. This is when data was added to the Navigator, not an "
                          "ISO 20022 publication.")


class MappingResult(BaseModel):
    found: bool
    input: str = Field(description="The message identifier as interpreted by the server")
    input_kind: Literal["iso20022", "legacy"]
    iso20022_message_name: str | None = Field(None, description="Name of the ISO 20022 message, when the input is one")
    iso20022_deactivated_in: str | None = Field(
        None, description="Navigator data load (quarter) in which the input message was marked "
                          "deactivated; absent if current")
    legacy_equivalents: list[Mapping] = Field(
        default_factory=list,
        description="Legacy or non-ISO messages (SWIFT MT, NACHA, CHAPS legacy) and their ISO 20022 equivalent",
    )
    used_by_schemes: list[Mapping] = Field(
        default_factory=list,
        description="Payment schemes that use this ISO 20022 message",
    )
    notes: list[str] = Field(default_factory=list, description="Caveats to pass on to the user")
    provenance: Provenance = Field(default_factory=Provenance)


# --------------------------------------------------------------------------
# SQL [1.1][1.2][1.3] — fixed strings, named placeholders, explicit columns.
# --------------------------------------------------------------------------
_SELECT = """
SELECT m.standard_id,
       s.display_name           AS standard_name,
       m.ref_id,
       m.ref_name,
       m.iso20022_message_id,
       t.message_name           AS iso20022_message_name,
       t.deactivated_in         AS iso20022_deactivated_in,
       m.status,
       m.description,
       m.mapping_type
FROM   i22_reference_message_map m
JOIN   i22_reference_standard    s ON s.standard_id = m.standard_id
-- Deactivated ISO messages are kept and flagged, not hidden [7.3]; the current row wins if both exist.
LEFT JOIN LATERAL (
    SELECT tt.message_name, tt.deactivated_in
    FROM   i22_message_type tt
    WHERE  tt.message_id = m.iso20022_message_id AND tt.variant = 1
    ORDER  BY tt.deactivated_in IS NULL DESC
    LIMIT  1
) t ON TRUE
"""
_STANDARD_FILTER = """
AND (%(std)s::text IS NULL
     OR lower(m.standard_id)  = lower(%(std)s)
     OR lower(s.display_name) = lower(%(std)s))
ORDER BY s.sort_order NULLS LAST, m.standard_id, m.ref_id
"""
SQL_BY_ISO = _SELECT + "WHERE m.iso20022_message_id = %(iso)s" + _STANDARD_FILTER
SQL_BY_LEGACY = (_SELECT + "WHERE m.mapping_type = 'EQUIVALENT' AND lower(m.ref_id) = ANY(%(cands)s)"
                 + _STANDARD_FILTER)
SQL_ISO_EXISTS = """
SELECT message_name, deactivated_in FROM i22_message_type
WHERE  message_id = %(iso)s AND variant = 1
ORDER  BY deactivated_in IS NULL DESC
LIMIT  1
"""
SQL_STANDARD_EXISTS = """
SELECT standard_id FROM i22_reference_standard
WHERE  lower(standard_id) = lower(%(s)s) OR lower(display_name) = lower(%(s)s)
"""
# Current data release for provenance [3.6]
# Baseline = earliest load. Revisit if a full data refresh is ever loaded.
SQL_RELEASE = "SELECT release_id FROM i22_release ORDER BY release_date ASC LIMIT 1"
SQL_STANDARDS = "SELECT standard_id FROM i22_reference_standard ORDER BY sort_order NULLS LAST, standard_id"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _respond(result: MappingResult) -> CallToolResult:
    """Send the result without null fields or empty lists [3.4]."""
    data = result.model_dump(mode="json", exclude_none=True)
    data = {k: v for k, v in data.items() if v != []}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
        structured_content=data,
    )


_RELEASE_TTL = 600          # seconds; data loads are rare, so a short cache saves a query per call
_release_cache: tuple[float, dict] | None = None


def _current_release() -> dict:
    """Baseline data load for provenance [3.6]. A failure here is logged, not fatal:
    the mapping answer is still valid without it, and the gap shows up in tests."""
    global _release_cache
    if _release_cache and time.monotonic() - _release_cache[0] < _RELEASE_TTL:
        return _release_cache[1]
    try:
        rows = fetch_all(SQL_RELEASE)
    except Exception:
        log.exception("could not read i22_release for provenance")
        return {}
    if not rows:
        return {}                          # not cached: an empty answer must not stick
    info = {"data_baseline": rows[0]["release_id"]}
    _release_cache = (time.monotonic(), info)
    return info


def _query(sql: str, params: dict) -> list[dict]:
    """Run a query; never let raw database errors reach the client [4.1]."""
    try:
        return fetch_all(sql, params)
    except Exception:
        log.exception("database error in iso20022_find_mappings")
        raise ToolError("The ISO 20022 reference database is temporarily unavailable. Try again in a minute.")


# --------------------------------------------------------------------------
# The tool
# --------------------------------------------------------------------------
def find_mappings(
    message: Annotated[
        str,
        Field(
            description=(
                "A legacy message (e.g. 'MT103', 'MT 202COV', '940', 'ACH Statement') or an "
                "ISO 20022 message (e.g. 'pacs.008', 'camt.053.001.08')."
            ),
            min_length=2,
            max_length=40,
            pattern=r"^[A-Za-z0-9 .\-/()]+$",
        ),
    ],
    standard: Annotated[
        str | None,
        Field(
            description=(
                "Optional: limit results to one standard or scheme, e.g. 'SWIFT_MT', 'SEPA', "
                "'NPP', 'CHAPS'. Leave empty to search all."
            ),
            max_length=30,
            pattern=r"^[A-Za-z0-9 _+/()\-]+$",
        ),
    ] = None,
) -> MappingResult:
    """Find which ISO 20022 message replaces a legacy payment message (e.g. MT103 -> pacs.008),
    and which payment schemes use an ISO 20022 message.

    Use when the user asks what replaces or corresponds to a legacy message (SWIFT MT,
    NACHA, CHAPS legacy MT), or which payment schemes use an ISO 20022 message
    (SEPA, NPP, CHAPS, Lynx, CIPS and others). Call it even when the answer seems well
    known: it returns sourced, current mappings with status and caveats.

    - Legacy input (MT103, 940, ACH Statement): returns its ISO 20022 equivalent(s)
      in legacy_equivalents.
    - ISO 20022 input (pacs.008): returns legacy_equivalents (legacy messages that map to
      it) and used_by_schemes (schemes that use it).

    Mappings are message-level only; this tool does not map individual fields.
    It does not describe the ISO 20022 message itself. ISO 20022 messages that have been
    deactivated are returned flagged with iso20022_deactivated_in, not hidden.
    """
    raw = " ".join(message.split())                        # collapse whitespace [2.3]
    std = " ".join(standard.split()) if standard else None
    notes: list[str] = []

    iso_match = ISO_ID.match(raw)
    if iso_match:
        kind = "iso20022"
        iso = iso_match.group(1).lower()
        if iso_match.group(2) or iso_match.group(3):
            notes.append(f"Mappings are held per message, not per version or variant; showing {iso}.")
        rows = _query(SQL_BY_ISO, {"iso": iso, "std": std})
        shown = iso
    else:
        kind = "legacy"
        cands = {raw.lower()}
        if m := MT_PREFIXED.match(raw):                    # MT103 -> also '103'
            cands |= {m.group(1).lower(), "mt" + m.group(1).lower()}
        elif MT_BARE.match(raw):                           # 103 -> also 'MT103'
            cands.add("mt" + raw.lower())
        rows = _query(SQL_BY_LEGACY, {"cands": sorted(cands), "std": std})
        shown = raw

    legacy: list[Mapping] = []
    schemes: list[Mapping] = []
    iso_name = None
    iso_deact = None
    deactivated: dict[str, str] = {}
    for r in rows:
        mapping = Mapping(**{k: r[k] for k in Mapping.model_fields})
        if kind == "iso20022":
            # The caller asked about this ISO message; state it once, not on every row.
            iso_name = iso_name or r["iso20022_message_name"]
            iso_deact = iso_deact or r["iso20022_deactivated_in"]
            mapping.iso20022_message_id = mapping.iso20022_message_name = None
            mapping.iso20022_deactivated_in = None
        if r["mapping_type"] == "USES":
            schemes.append(mapping)
        else:
            legacy.append(mapping)
        if r["iso20022_deactivated_in"]:
            deactivated[r["iso20022_message_id"]] = r["iso20022_deactivated_in"]
        if r["mapping_type"] == "EQUIVALENT" and not r["iso20022_message_id"]:
            notes.append(f"{r['standard_name']} {r['ref_id']} has no ISO 20022 equivalent in this dataset.")

    for msg, rel in deactivated.items():
        notes.append(f"{msg} was marked deactivated in the Navigator's {rel} data load; "
                     "it is no longer in the current catalogue.")

    # Explicit, explained not-found [3.5][7.1]
    if not rows:
        if std and not _query(SQL_STANDARD_EXISTS, {"s": std}):
            valid = ", ".join(r["standard_id"] for r in _query(SQL_STANDARDS, {}))
            notes.append(f"Unknown standard '{std}'. Valid values: {valid}.")
        elif kind == "iso20022":
            exists = _query(SQL_ISO_EXISTS, {"iso": shown})
            if exists and exists[0]["deactivated_in"]:
                iso_name, iso_deact = exists[0]["message_name"], exists[0]["deactivated_in"]
                notes.append(f"{shown} ({iso_name}) was marked deactivated in the Navigator's {iso_deact} data "
                             "load; no legacy "
                             "equivalents or scheme usage are recorded for it"
                             + (f" in {std}." if std else "."))
            elif exists:
                notes.append(f"{shown} ({exists[0]['message_name']}) is an ISO 20022 message, but no legacy "
                             "equivalents or scheme usage are recorded for it"
                             + (f" in {std}." if std else "."))
            else:
                notes.append(f"{shown} is not an ISO 20022 message in this dataset.")
        elif kind == "legacy" and (match := _query(SQL_STANDARD_EXISTS, {"s": shown})):
            # The caller passed a standard (e.g. 'iDEAL') where a message was expected.
            sid = match[0]["standard_id"]
            notes.append(f"'{shown}' is a payment standard ({sid}), not a message. To check a message in it, "
                         f"call again with message set to the message (e.g. 'pacs.008') and standard='{sid}'. "
                         "Listing every message a standard uses is not supported by this tool.")
        else:
            notes.append(f"No mapping recorded for '{shown}'"
                         + (f" in {std}" if std else "")
                         + ". Do not infer an equivalent from general knowledge.")

    return _respond(MappingResult(
        found=bool(rows), input=shown, input_kind=kind, iso20022_message_name=iso_name,
        iso20022_deactivated_in=iso_deact,
        provenance=Provenance(**_current_release()),
        legacy_equivalents=legacy, used_by_schemes=schemes, notes=notes,
    ))
