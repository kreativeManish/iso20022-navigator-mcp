"""
code_sets.py — the iso20022_code_sets tool.

Answers three kinds of question about ISO 20022 external code sets, from i22_codeset,
i22_code_value and i22_codeset_message:
  - a code set:  "What does BalanceType contain?"    -> its values, and the messages that use it
  - a message:   "Which code sets does camt.053 use?" -> the code sets, with their value counts
  - a code:      "What does CLBD mean?"               -> every code set that contains it

The Bank Transaction Code concepts (BankTransactionDomain / Family / SubFamily) belong to
iso20022_bank_transaction_codes and are left out of lookups here.

Long lists (values, code sets, matches) are sent as pipe-delimited text with a header row,
not as JSON objects: that is about a quarter of the size. The rest of the response stays
structured (found, notes, provenance, set attributes). Lists are paged by whole rows, so a
description is never cut in two.

Release checklist references are in square brackets, e.g. [1.2].
"""
import json
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from mappings import ISO_ID, SQL_ISO_EXISTS
from shared import Provenance, current_release, query, respond

TOOL_NAME = "iso20022_code_sets"
BTC_TOOL_NAME = "iso20022_bank_transaction_codes"
MAPPINGS_TOOL_NAME = "iso20022_legacy_and_scheme_mappings"

# Bank Transaction Code concepts: served by the BTC tool, excluded here. Lower case, for comparison.
BTC_CONCEPTS = ("BankTransactionDomain", "BankTransactionFamily", "BankTransactionSubFamily")
_BTC_LOWER = [c.lower() for c in BTC_CONCEPTS]

# Size control [3.2]. The whole response stays under RESPONSE_BUDGET characters. The envelope
# (set attributes, message list, notes, provenance) is measured first; the list rows get what is
# left, up to VALUE_CAP. PAGING_RESERVE covers the page object and the paging notes, which are
# added afterwards. At least one row is always returned, so paging always advances.
RESPONSE_BUDGET = 20_000      # raised from 16,000 on 2026-10-12; confirm with real clients (check 6.4, Q6 and Q7)
VALUE_CAP = 16_000
PAGING_RESERVE = 700
MIN_VALUE_BUDGET = 2_000

HEADER_VALUES = "code|name|description|deactivated_in"
HEADER_CODE_SETS = "concept|navigator_group|code_count"
HEADER_MATCHES = "concept|code|name|description|deactivated_in"

CODE_SETS_SCOPE = ("External code sets and their values as loaded in the Navigator. Links between code sets and "
                   "messages are message-level, not element-level. Bank Transaction Code (BTC) concepts are "
                   f"served by {BTC_TOOL_NAME}. Value descriptions are ISO text; code set descriptions "
                   "(concept_description) are AI-generated.")

AI_NOTE = "concept_description is AI-generated."
GROUP_NOTE = "navigator_group is the Navigator's own grouping of code sets, not an ISO classification."
BTC_POINTER = (f"Bank Transaction Code (BTC) concepts (BankTransactionDomain, BankTransactionFamily, "
               f"BankTransactionSubFamily) are served by {BTC_TOOL_NAME}.")
FOREX_GAP = "Some Forex code sets are known to be missing from this dataset, so this answer may be incomplete."


# --------------------------------------------------------------------------
# Output models [3.1] — only fields declared here can leave the server.
# --------------------------------------------------------------------------
class CodeSetInfo(BaseModel):
    concept: str = Field(description="Short code set name, e.g. BalanceType")
    name: str | None = Field(None, description="Full ISO name of the code set, e.g. ExternalBalanceType1Code")
    navigator_group: str | None = Field(
        None, description="The Navigator's own grouping of code sets (13 groups); not an ISO classification")
    concept_description: str | None = Field(None, description="AI-generated description of the code set")
    active_values: int = Field(description="Number of current values in the code set")
    deactivated_values: int | None = Field(
        None, description="Number of values marked deactivated; absent if none")
    deactivated_in: str | None = Field(
        None, description="Navigator data load (quarter) in which the code set was marked deactivated; "
                          "absent if current")


class Page(BaseModel):
    offset: int = Field(description="Row number this page starts at (0 = first row)")
    returned: int = Field(description="Rows on this page")
    total: int = Field(description="Rows in the whole list")
    complete: bool = Field(description="True when this page ends the list")
    next_offset: int | None = Field(None, description="Offset for the next page; absent when complete")


class CodeSetsResult(BaseModel):
    found: bool
    input: str = Field(description="The identifier as interpreted by the server: the code set's short name, "
                                   "the ISO 20022 message, or the code")
    input_kind: Literal["code_set", "message", "code", "unknown"]
    code_set: CodeSetInfo | None = Field(None, description="Attributes of the code set (input_kind code_set)")
    iso20022_message_name: str | None = Field(
        None, description="Name of the ISO 20022 message, when the input is one")
    iso20022_deactivated_in: str | None = Field(
        None, description="Navigator data load (quarter) in which the input message was marked deactivated; "
                          "absent if current")
    values: str | None = Field(
        None, description=f"input_kind code_set: the values, pipe-delimited, one row per line. First line is the "
                          f"header: {HEADER_VALUES}. deactivated_in is empty for current values.")
    used_in_messages: list[str] = Field(
        default_factory=list,
        description="input_kind code_set: IDs of the messages (all variants) that use this code set. "
                    "Complete on every page.")
    code_sets: str | None = Field(
        None, description=f"input_kind message: the code sets the message uses, pipe-delimited, one row per line. "
                          f"First line is the header: {HEADER_CODE_SETS}. code_count is the number of current values.")
    matches: str | None = Field(
        None, description=f"input_kind code: every code set containing the code, pipe-delimited, one row per line. "
                          f"First line is the header: {HEADER_MATCHES}.")
    page: Page | None = Field(
        None, description="Paging of the pipe-delimited list. Continue with next_offset until complete is true.")
    notes: list[str] = Field(default_factory=list, description="Caveats to pass on to the user")
    provenance: Provenance


# --------------------------------------------------------------------------
# SQL [1.1][1.2][1.3] — fixed strings, named placeholders, explicit columns.
# Lists are ordered with the "C" collation so paging is stable.
# --------------------------------------------------------------------------
SQL_CODE_SET = """
SELECT concept, codeset_name, group_name, description, deactivated_in
FROM   i22_codeset
WHERE  (lower(concept) = lower(%(s)s) OR lower(codeset_name) = lower(%(s)s))
AND    lower(concept) <> ALL(%(btc)s)
ORDER  BY (lower(concept) = lower(%(s)s)) DESC
LIMIT  1
"""
SQL_BTC_CONCEPT = """
SELECT concept FROM i22_codeset
WHERE  (lower(concept) = lower(%(s)s) OR lower(codeset_name) = lower(%(s)s))
AND    lower(concept) = ANY(%(btc)s)
LIMIT  1
"""
SQL_VALUE_COUNTS = """
SELECT count(*) FILTER (WHERE deactivated_in IS NULL)     AS active,
       count(*) FILTER (WHERE deactivated_in IS NOT NULL) AS deactivated
FROM   i22_code_value
WHERE  concept = %(c)s
"""
# Deactivated values are kept and flagged, not hidden [7.3]. The optional code filter is one slot.
SQL_VALUES = """
SELECT code, code_name, description, deactivated_in
FROM   i22_code_value
WHERE  concept = %(c)s
AND    (%(code)s::text IS NULL OR lower(code) = lower(%(code)s))
ORDER  BY code COLLATE "C"
"""
SQL_SET_MESSAGES = """
SELECT message_id
FROM   i22_codeset_message
WHERE  concept = %(c)s
GROUP  BY message_id
ORDER  BY message_id COLLATE "C"
"""
SQL_MESSAGE_SETS = """
SELECT cm.concept,
       cs.group_name AS navigator_group,
       (SELECT count(*) FROM i22_code_value v
        WHERE v.concept = cm.concept AND v.deactivated_in IS NULL) AS code_count
FROM   i22_codeset_message cm
JOIN   i22_codeset cs ON cs.concept = cm.concept
WHERE  cm.message_id = %(iso)s AND cm.variant = 1
ORDER  BY cm.concept COLLATE "C"
"""
SQL_CODE_MATCHES = """
SELECT concept, code, code_name, description, deactivated_in
FROM   i22_code_value
WHERE  lower(code) = lower(%(code)s)
AND    lower(concept) <> ALL(%(btc)s)
ORDER  BY concept COLLATE "C"
"""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _q(sql: str, params: dict) -> list[dict]:
    return query(TOOL_NAME, sql, params)


def _cell(value) -> str:
    """One field of a delimited row. Pipes, tabs, newlines and runs of spaces become one space,
    so a value can never break the row layout. (The data has none today; a test fails if it does.)"""
    return "" if value is None else " ".join(str(value).replace("|", " ").split())


def _row(*cells) -> str:
    return "|".join(_cell(c) for c in cells)


def _provenance() -> Provenance:
    return Provenance(scope=CODE_SETS_SCOPE, **current_release())


def _attach_page(res: CodeSetsResult, field: str, header: str, rows: list[str], offset: int) -> int:
    """Fill `field` with the header and as many whole rows from `offset` as fit, and set the page
    object and paging notes. The row budget is what remains of RESPONSE_BUDGET after everything
    else in the response. Returns the number of rows on the page (at least 1 when rows remain)."""
    setattr(res, field, header)
    envelope = len(respond(res).content[0].text)
    budget = max(MIN_VALUE_BUDGET, min(VALUE_CAP, RESPONSE_BUDGET - envelope - PAGING_RESERVE))
    used = n = 0
    for row in rows[offset:]:
        cost = len(json.dumps(row, ensure_ascii=False))     # its quotes stand in for the "\n" before the row
        if n and used + cost > budget:
            break
        used += cost
        n += 1
    end = offset + n
    total = len(rows)
    setattr(res, field, "\n".join([header, *rows[offset:end]]))
    complete = end >= total
    res.page = Page(offset=offset, returned=n, total=total, complete=complete,
                    next_offset=None if complete else end)
    if total and offset >= total:
        res.notes.append(f"offset {offset} is past the end of the list, which has {total} rows "
                         f"(offsets 0 to {total - 1}).")
    elif not complete:
        res.notes.append(f"Rows {offset + 1}-{end} of {total} are shown. This is not the complete list: "
                         f"call again with the same input and offset={end} for the next page.")
    elif offset:
        res.notes.append(f"Rows {offset + 1}-{end} of {total}: this is the final page.")
    return n


def _deactivated_note(subject: str) -> str:
    return (f"This page includes deactivated {subject}: deactivated_in holds the Navigator data load (quarter) "
            "in which each was marked deactivated, so it is no longer in the current ISO list.")


def _code_ignored_note() -> str:
    return "The code parameter applies only when message is a code set; it was ignored."


# --------------------------------------------------------------------------
# The three kinds of input
# --------------------------------------------------------------------------
def _by_code_set(cs: dict, code_filter: str | None, offset: int) -> CodeSetsResult:
    concept = cs["concept"]
    counts = _q(SQL_VALUE_COUNTS, {"c": concept})[0]
    data = _q(SQL_VALUES, {"c": concept, "code": code_filter})
    info = CodeSetInfo(
        concept=concept, name=cs["codeset_name"], navigator_group=cs["group_name"],
        concept_description=cs["description"], active_values=counts["active"],
        deactivated_values=counts["deactivated"] or None, deactivated_in=cs["deactivated_in"])
    notes: list[str] = []
    if cs["description"]:
        notes.append(AI_NOTE)
    if cs["group_name"]:
        notes.append(GROUP_NOTE)
    if cs["deactivated_in"]:
        notes.append(f"The code set {concept} was marked deactivated in the Navigator's {cs['deactivated_in']} "
                     "data load; it is no longer in the current catalogue.")
    res = CodeSetsResult(found=True, input=concept, input_kind="code_set", code_set=info,
                         notes=notes, provenance=_provenance())
    notes = res.notes                              # pydantic copies the list it is given

    if code_filter and not data:                   # explicit, explained not-found [3.5]
        res.found = False
        notes.append(f"'{code_filter}' is not a value of {concept}. Do not infer one from general knowledge.")
        return res

    messages = [r["message_id"] for r in _q(SQL_SET_MESSAGES, {"c": concept})]
    res.used_in_messages = messages
    if messages:
        notes.append("used_in_messages lists message IDs across all variants; usage is recorded per message, "
                     "not per element.")
    else:
        notes.append(f"No messages are recorded as using {concept} in this dataset.")
    if code_filter:
        notes.append(f"Only the value matching '{code_filter}' is returned; call without code for the whole set.")

    rows = [_row(r["code"], r["code_name"], r["description"], r["deactivated_in"]) for r in data]
    n = _attach_page(res, "values", HEADER_VALUES, rows, offset)
    if any(r["deactivated_in"] for r in data[offset:offset + n]):
        notes.append(_deactivated_note("values"))
    return res


def _by_message(iso: str, qualified: bool, offset: int) -> CodeSetsResult:
    rows = _q(SQL_MESSAGE_SETS, {"iso": iso})
    exists = _q(SQL_ISO_EXISTS, {"iso": iso})
    name = exists[0]["message_name"] if exists else None
    deactivated = exists[0]["deactivated_in"] if exists else None
    notes: list[str] = []
    if qualified:
        notes.append(f"Code set usage is held per message, not per version or variant; showing {iso}.")
    res = CodeSetsResult(found=bool(rows), input=iso, input_kind="message", iso20022_message_name=name,
                         iso20022_deactivated_in=deactivated, notes=notes, provenance=_provenance())
    notes = res.notes                              # pydantic copies the list it is given
    label = f"{iso} ({name})" if name else iso
    if deactivated:
        notes.append(f"{label} is a recognised ISO 20022 message that was retired in the Navigator's "
                     f"{deactivated} data load; it is no longer in the current catalogue.")
    if not rows:
        if exists:
            notes.append(f"{label} is an ISO 20022 message, but no code sets are recorded for it.")
        else:
            notes.append(f"{iso} is not an ISO 20022 message in this dataset.")
        notes.append("Do not infer code sets from general knowledge.")
        if iso.startswith("fxtr"):
            notes.append(FOREX_GAP)
        return res

    notes.append(f"These are the code sets linked to {iso} (variant 1), at message level, not element level. "
                 "code_count is the number of current values in each set.")
    notes.append(GROUP_NOTE)
    if any(r["concept"].lower() in _BTC_LOWER for r in rows):
        notes.append(f"BTC concepts are listed here with their value counts; their values are served by "
                     f"{BTC_TOOL_NAME}, not by this tool.")
    if iso.startswith("fxtr"):
        notes.append(FOREX_GAP)
    _attach_page(res, "code_sets", HEADER_CODE_SETS,
                 [_row(r["concept"], r["navigator_group"], r["code_count"]) for r in rows], offset)
    return res


def _by_code(raw: str, data: list[dict], offset: int) -> CodeSetsResult:
    concepts = {r["concept"] for r in data}
    notes = [f"Bank Transaction Code (BTC) values are not searched here; they are served by {BTC_TOOL_NAME}."]
    if len(concepts) > 1:
        notes.append(f"The code {raw} exists in {len(concepts)} code sets; choose the one that fits the question.")
    res = CodeSetsResult(found=True, input=raw, input_kind="code", notes=notes, provenance=_provenance())
    notes = res.notes                              # pydantic copies the list it is given
    rows = [_row(r["concept"], r["code"], r["code_name"], r["description"], r["deactivated_in"]) for r in data]
    n = _attach_page(res, "matches", HEADER_MATCHES, rows, offset)
    if any(r["deactivated_in"] for r in data[offset:offset + n]):
        notes.append(_deactivated_note("matches"))
    return res


def _not_found(raw: str) -> CodeSetsResult:
    notes: list[str] = []
    if _q(SQL_BTC_CONCEPT, {"s": raw, "btc": _BTC_LOWER}):
        notes.append(f"'{raw}' is a Bank Transaction Code (BTC) concept. This tool does not serve BTC; "
                     f"use {BTC_TOOL_NAME}.")
    else:
        notes.append(f"No code set, code or ISO 20022 message matches '{raw}'. Do not infer an answer from "
                     "general knowledge.")
        notes.append(BTC_POINTER)
        notes.append(f"For which ISO 20022 message replaces a legacy message, use {MAPPINGS_TOOL_NAME}.")
        notes.append(FOREX_GAP)
    return CodeSetsResult(found=False, input=raw, input_kind="unknown", notes=notes, provenance=_provenance())


# --------------------------------------------------------------------------
# The tool
# --------------------------------------------------------------------------
def find_code_sets(
    message: Annotated[
        str,
        Field(
            description=(
                "A code set (e.g. 'BalanceType', or its full name 'ExternalBalanceType1Code'), a code "
                "(e.g. 'CLBD', 'OTHR'), or an ISO 20022 message (e.g. 'camt.053', 'pacs.008.001.08')."
            ),
            min_length=1,
            max_length=60,
            pattern=r"^[A-Za-z0-9 .\-/()_]+$",
        ),
    ],
    code: Annotated[
        str | None,
        Field(
            description=(
                "Optional, only when message is a code set: check one code in that set, e.g. message "
                "'BalanceType' with code 'CLBD'. Returns just that value, so use it for 'is X a valid ...' "
                "questions instead of fetching the whole set."
            ),
            max_length=10,
            pattern=r"^[A-Za-z0-9 ._+\-/()]*$",       # empty allowed: treated as not given
        ),
    ] = None,
    offset: Annotated[
        int,
        Field(
            description=(
                "Row to start from; 0 (the default) is the first row. Use only to continue a list whose "
                "previous response said it was not complete, with the next_offset from that response."
            ),
            ge=0,
            le=100_000,
        ),
    ] = 0,
) -> CodeSetsResult:
    """Use for ANY question about ISO 20022 external code sets: the values of a code set (BalanceType,
    ReturnReason, Purpose, StatusReason, ...), what a code means (CLBD, OTHR, ...), whether a code is
    valid in a code set, which code sets a message uses (camt.053, pain.001, ...), or which messages
    use a code set. Call it even when the answer seems well known: it returns the sourced, current
    values with their official descriptions and flags deactivated ones.

    Send one identifier in `message`:
    - A code set (BalanceType, or ExternalBalanceType1Code): its description, its values (code, name,
      description) and the messages that use it. Add `code` to check a single value.
    - A code (CLBD): every code set that contains it, with the name and description in each.
    - An ISO 20022 message (camt.053): the code sets it uses, with their number of values.

    The values, code sets and matches are pipe-delimited text with a header row. Long lists are paged
    by whole rows: when `page.complete` is false, call again with the same input and the offset in
    `page.next_offset` until it is true, before answering about the whole list.

    Not covered: Bank Transaction Codes (PMNT, PMNT-RCDT-ESCT; use iso20022_bank_transaction_codes),
    which ISO 20022 message replaces a legacy message or which schemes use one (use
    iso20022_legacy_and_scheme_mappings), and what a message itself contains. Code set usage is
    message-level, not element-level. The code set description (concept_description) is AI-generated.
    """
    raw = " ".join(message.split())                        # collapse whitespace [2.3]
    code_filter = (" ".join(code.split()) if code else None) or None

    if m := ISO_ID.match(raw):
        res = _by_message(m.group(1).lower(), bool(m.group(2) or m.group(3)), offset)
    elif cs := _q(SQL_CODE_SET, {"s": raw, "btc": _BTC_LOWER}):
        return respond(_by_code_set(cs[0], code_filter, offset))
    elif data := _q(SQL_CODE_MATCHES, {"code": raw, "btc": _BTC_LOWER}):
        res = _by_code(raw, data, offset)
    else:
        res = _not_found(raw)
    if code_filter:
        res.notes.append(_code_ignored_note())
    return respond(res)
