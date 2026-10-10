"""
bank_transaction_codes.py — the iso20022_bank_transaction_codes tool.

Answers questions about ISO 20022 Bank Transaction Codes (BTC), the Domain / Family /
SubFamily codes that classify entries in bank statements and account reports, from
i22_btc_combinations (the valid triples) and i22_code_value (the official names).

One input, `code`, covers every question: a domain, a domain-family pair, a full path,
a family or subfamily alone (leading slashes fix the level), a pair with a skipped
level, or a bare code matched at every level.

Release checklist references are in square brackets, e.g. [1.2].
"""
import re
from typing import Annotated, Callable, Literal

from pydantic import BaseModel, Field

from mcp.server.mcpserver.exceptions import ToolError

from shared import Provenance, current_release, query, respond

TOOL_NAME = "iso20022_bank_transaction_codes"

LEVELS = ("domain", "family", "subfamily")
CONCEPT = {"domain": "BankTransactionDomain", "family": "BankTransactionFamily",
           "subfamily": "BankTransactionSubFamily"}
PLURAL = {"domain": "domains", "family": "families", "subfamily": "subfamilies"}

MAX_LISTED_ITEMS = 100      # listed codes across all lists and matches; biggest case today is about 81
MAX_CLOSEST_PATHS = 5

BTC_SCOPE = ("ISO external Bank Transaction Code list only (domain, family, subfamily). "
             "Bank-specific (proprietary) codes are not included.")

ACCEPTED_FORMS = ("Accepted forms: PMNT, PMNT-RCDT, PMNT-RCDT-ESCT, /RCDT (a family), //ESCT (a subfamily), "
                  "/RCDT/ESCT, PMNT//ESCT, or a single code such as ESCT. Each part is four letters or digits.")


# --------------------------------------------------------------------------
# Output models [3.1] — only fields declared here can leave the server.
# --------------------------------------------------------------------------
class Code(BaseModel):
    code: str
    name: str


class Resolved(BaseModel):
    domain: Code | None = None
    family: Code | None = None
    subfamily: Code | None = None


class Counts(BaseModel):
    domains: int
    families: int
    subfamilies: int
    combinations: int = Field(description="Valid domain-family-subfamily combinations that match")


class Match(BaseModel):
    level: Literal["domain", "family", "subfamily", "combination"] = Field(
        description="The deepest level the request fixed; 'combination' when all three were given")
    resolved: Resolved = Field(description="The codes the request fixed, with their official names")
    counts: Counts | None = Field(None, description="Totals over all matching combinations (not only those listed)")
    domains: list[Code] | None = None
    families: list[Code] | None = None
    subfamilies: list[Code] | None = None
    all_pairs_exist: bool | None = Field(
        None, description="Present when two lists are returned. True: every item in one list combines with every "
                          "item in the other. False: the lists are not paired; some pairs do not exist.")
    description: str | None = Field(
        None, description="AI-generated description of the combination, validated against ISO definitions; "
                          "present when exactly one combination matches")


class Segment(BaseModel):
    code: str
    exists_as: list[Literal["domain", "family", "subfamily"]] | None = Field(
        None, description="Levels at which this code exists in the ISO list; absent if it exists nowhere")


class BankTransactionCodeResult(BaseModel):
    found: bool
    input: str = Field(description="The code as interpreted by the server, slash-separated by level, "
                                   "e.g. PMNT/RCDT, /RCDT, //ESCT")
    matches: list[Match] = Field(default_factory=list)
    segments: list[Segment] = Field(
        default_factory=list, description="Not found only: where each part of the input exists in the ISO list")
    closest_paths: list[str] = Field(
        default_factory=list, description="Not found only: up to 5 existing paths sharing a part with the input")
    notes: list[str] = Field(default_factory=list, description="Caveats to pass on to the user")
    provenance: Provenance


# --------------------------------------------------------------------------
# SQL [1.1][1.2][1.3] — fixed strings, named placeholders, explicit columns.
# Names come from i22_code_value through inner joins: foreign keys guarantee the rows exist.
# --------------------------------------------------------------------------
_SELECT = """
SELECT c.domain, c.family, c.subfamily,
       dn.code_name AS domain_name,
       fn.code_name AS family_name,
       sn.code_name AS subfamily_name
FROM   i22_btc_combinations c
JOIN   i22_code_value dn ON dn.concept = 'BankTransactionDomain'    AND dn.code = c.domain
JOIN   i22_code_value fn ON fn.concept = 'BankTransactionFamily'    AND fn.code = c.family
JOIN   i22_code_value sn ON sn.concept = 'BankTransactionSubFamily' AND sn.code = c.subfamily
"""
_ORDER = "ORDER BY c.domain, c.family, c.subfamily"

# Each of the three slots is an optional filter.
SQL_BY_SLOTS = _SELECT + """
WHERE  (%(d)s::text IS NULL OR c.domain    = %(d)s)
AND    (%(f)s::text IS NULL OR c.family    = %(f)s)
AND    (%(s)s::text IS NULL OR c.subfamily = %(s)s)
""" + _ORDER

# A bare code: matched at every level.
SQL_BY_ANY = _SELECT + """
WHERE  c.domain = %(x)s OR c.family = %(x)s OR c.subfamily = %(x)s
""" + _ORDER

# The description of one combination; asked for only when a request resolves to a single one.
SQL_DESCRIPTION = """
SELECT description FROM i22_btc_combinations
WHERE  domain = %(d)s AND family = %(f)s AND subfamily = %(s)s
"""

# Where each part of a not-found input exists, at any level.
SQL_SEGMENTS = """
SELECT code, concept
FROM   i22_code_value
WHERE  code = ANY(%(codes)s)
AND    concept IN ('BankTransactionDomain', 'BankTransactionFamily', 'BankTransactionSubFamily')
"""

# Existing paths that share at least one part, in its own position, with the input.
SQL_CLOSEST = """
SELECT c.domain, c.family, c.subfamily, count(*) OVER () AS total
FROM   i22_btc_combinations c
WHERE  c.domain = %(d)s OR c.family = %(f)s OR c.subfamily = %(s)s
ORDER  BY COALESCE((c.domain = %(d)s)::int, 0) + COALESCE((c.family = %(f)s)::int, 0)
        + COALESCE((c.subfamily = %(s)s)::int, 0) DESC,
          c.domain, c.family, c.subfamily
LIMIT  %(limit)s
"""


# --------------------------------------------------------------------------
# Input reading
# --------------------------------------------------------------------------
_PART = re.compile(r"^[A-Z0-9]{4}$")
_SEPARATOR = re.compile(r"\s*[,.\-_]\s*|\s+")        # hyphen, comma, dot, underscore or space


def _shape_error(code: str) -> ToolError:
    return ToolError(f"'{code}' is not a bank transaction code shape. {ACCEPTED_FORMS}")


def _parse(code: str) -> tuple[tuple[str | None, str | None, str | None], bool]:
    """Return ((domain, family, subfamily), bare). Normalises case and separators [2.3].
    bare is True for a single code with no level given."""
    text = " ".join(code.upper().split())
    if "/" in text:
        parts = [p.strip() for p in text.split("/")]
        if len(parts) > 3 or not any(parts) or any(p and not _PART.match(p) for p in parts):
            raise _shape_error(code)
        slots = tuple((p or None) for p in parts + [""] * (3 - len(parts)))
        return slots, False                          # type: ignore[return-value]
    parts = _SEPARATOR.split(text)
    if len(parts) > 3 or any(not _PART.match(p) for p in parts):
        raise _shape_error(code)
    if len(parts) == 1:
        return (None, None, None), True
    return tuple(parts + [None] * (3 - len(parts))), False      # type: ignore[return-value]


def _canonical(slots: tuple[str | None, str | None, str | None]) -> str:
    return "/".join(s or "" for s in slots).rstrip("/")


# --------------------------------------------------------------------------
# Building a match
# --------------------------------------------------------------------------
class _Budget:
    """Listed items still allowed in this response [3.2]."""

    def __init__(self, limit: int):
        self.left = limit
        self.total = 0
        self.shown = 0

    def take(self, items: list[Code]) -> list[Code]:
        self.total += len(items)
        kept = items[:max(self.left, 0)]
        self.left -= len(kept)
        self.shown += len(kept)
        return kept


def _distinct(rows: list[dict], level: str) -> list[Code]:
    names = {r[level]: r[level + "_name"] for r in rows}
    return [Code(code=c, name=names[c]) for c in sorted(names)]


def _build_match(slots: dict[str, str | None], rows: list[dict], budget: _Budget, notes: list[str],
                 describe: Callable[[dict], str | None]) -> Match:
    fixed = [lv for lv in LEVELS if slots[lv]]
    level = "combination" if len(fixed) == 3 else fixed[-1]
    resolved = Resolved(**{lv: Code(code=slots[lv], name=rows[0][lv + "_name"]) for lv in fixed})
    distinct = {lv: _distinct(rows, lv) for lv in LEVELS}
    match = Match(level=level, resolved=resolved)
    if len(rows) == 1:
        match.description = describe(rows[0])
    if level != "combination":
        match.counts = Counts(domains=len(distinct["domain"]), families=len(distinct["family"]),
                              subfamilies=len(distinct["subfamily"]), combinations=len(rows))
    # Lists: the levels the request left open. A domain alone lists only its families (next level down).
    listed = [lv for lv in LEVELS if not slots[lv]]
    if fixed == ["domain"]:
        listed = ["family"]
    for lv in listed:
        setattr(match, PLURAL[lv], budget.take(distinct[lv]) or None)
    if len(listed) == 2:
        a, b = listed
        possible = len(distinct[a]) * len(distinct[b])
        match.all_pairs_exist = len(rows) == possible
        if not match.all_pairs_exist:
            notes.append(f"The {PLURAL[a]} and {PLURAL[b]} listed for {_label(slots)} are not paired: only "
                         f"{len(rows)} of {possible} possible {a}-{b} pairs exist. To check one pair, call "
                         "again with the full path, e.g. PMNT-RCDT-ESCT.")
    return match


def _label(slots: dict[str, str | None]) -> str:
    return "/".join(slots[lv] or "" for lv in LEVELS).rstrip("/")


# --------------------------------------------------------------------------
# The tool
# --------------------------------------------------------------------------
def find_transaction_codes(
    code: Annotated[
        str,
        Field(
            description=(
                "A Bank Transaction Code or part of one, written domain-family-subfamily with hyphens, e.g. "
                "'PMNT-RCDT-ESCT'. Each part is four letters or digits. Begin with slashes to fix the level "
                "('/RCDT' is a family, '//ESCT' is a subfamily) and leave a level empty to skip it "
                "('PMNT//ESCT'). Slash, comma or space also work as separators."
            ),
            min_length=1,
            max_length=30,
            pattern=r"^[A-Za-z0-9 ,./_\-]+$",
        ),
    ],
) -> BankTransactionCodeResult:
    """Use for ANY question about ISO 20022 Bank Transaction Codes (BTC): the Domain / Family /
    SubFamily codes that classify entries in bank statements and account reports, such as PMNT,
    PMNT-RCDT or PMNT-RCDT-ESCT. Call it even when the code looks familiar: it returns the
    official names, descriptions and how the codes fit together.

    Send the code in one of these forms:
    - PMNT (a domain): its name and its families.
    - PMNT-RCDT (domain and family): both names and its subfamilies.
    - PMNT-RCDT-ESCT (all three): the three names and an explanation of the combination.
    - /RCDT (a family): its name, the domains it appears in and its subfamilies.
    - //ESCT (a subfamily): its name, the domains and families it appears in.
    - /RCDT/ESCT (family and subfamily): the domains where this pair exists.
    - PMNT//ESCT (domain and subfamily): the families under PMNT that contain ESCT.
    - ESCT (no level given): matched as a domain, a family and a subfamily; each level
      where it exists is returned separately.

    This tool matches codes, not meanings. To find the code for a type of transaction, browse
    from the domain down: PMNT, then its families, then their subfamilies. It covers the ISO
    external code list only; bank-specific (proprietary) codes are outside it. It has no
    information about message structure or how a bank fills in a code.
    """
    slots_t, bare = _parse(code)
    notes: list[str] = []
    budget = _Budget(MAX_LISTED_ITEMS)
    matches: list[Match] = []

    def run(sql: str, params: dict) -> list[dict]:
        return query(TOOL_NAME, sql, params)

    def describe(row: dict) -> str | None:
        found = run(SQL_DESCRIPTION, {"d": row["domain"], "f": row["family"], "s": row["subfamily"]})
        return found[0]["description"] if found else None

    if bare:
        x = _SEPARATOR.split(" ".join(code.upper().split()))[0]
        shown_input = x
        slot_map = {lv: None for lv in LEVELS}
        found_rows = run(SQL_BY_ANY, {"x": x})
        for lv in LEVELS:                                   # each level where the code exists, separately
            at_level = [r for r in found_rows if r[lv] == x]
            if at_level:
                matches.append(_build_match({**slot_map, lv: x}, at_level, budget, notes, describe))
        if len(matches) > 1:
            levels = [m.level for m in matches]
            pin = {"domain": f"'{x}/'", "family": f"'/{x}'", "subfamily": f"'//{x}'"}
            notes.append(f"{x} exists at more than one level ({', '.join(levels)}); each is returned separately. "
                         "To ask for one level, send " + ", ".join(f"{pin[lv]} for a {lv}" for lv in levels) + ".")
    else:
        slot_map = dict(zip(LEVELS, slots_t, strict=True))
        shown_input = _canonical(slots_t)
        found_rows = run(SQL_BY_SLOTS, {"d": slots_t[0], "f": slots_t[1], "s": slots_t[2]})
        if found_rows:
            matches.append(_build_match(slot_map, found_rows, budget, notes, describe))

    if budget.total > budget.shown:
        notes.append(f"Showing {budget.shown} of {budget.total} listed items, ordered by code. "
                     "Narrow the request, for example by adding a domain or family (PMNT-RCDT).")

    segments: list[Segment] = []
    closest: list[str] = []
    if not found_rows:
        given = [x] if bare else [c for c in slots_t if c]
        levels_of: dict[str, list[str]] = {}
        for r in run(SQL_SEGMENTS, {"codes": given}):
            lv = next(k for k, v in CONCEPT.items() if v == r["concept"])
            levels_of.setdefault(r["code"], []).append(lv)
        for c in given:
            segments.append(Segment(code=c, exists_as=[lv for lv in LEVELS if lv in levels_of.get(c, [])] or None))
        unknown = [s.code for s in segments if s.exists_as is None]
        for c in unknown:
            notes.append(f"{c} is not in the ISO external code list.")
        if not bare:
            for lv, c in slot_map.items():
                if c and c in levels_of and lv not in levels_of[c]:
                    notes.append(f"{c} is not a {lv} code; it exists as a {' and a '.join(levels_of[c])}.")
            if not unknown:
                notes.append(f"{shown_input} is not a combination in the ISO external code list, although each "
                             "part exists (see segments).")
            near = run(SQL_CLOSEST, {"d": slots_t[0], "f": slots_t[1], "s": slots_t[2],
                                     "limit": MAX_CLOSEST_PATHS})
            closest = [f"{r['domain']}-{r['family']}-{r['subfamily']}" for r in near]
            if near and near[0]["total"] > len(near):
                notes.append(f"Showing {len(near)} of {near[0]['total']} closest paths.")
        notes.append("A code that is not found may be a bank-specific code outside the ISO list. "
                     "Do not infer a meaning from general knowledge.")

    return respond(BankTransactionCodeResult(
        found=bool(found_rows), input=shown_input, matches=matches, segments=segments,
        closest_paths=closest, notes=notes, provenance=Provenance(scope=BTC_SCOPE, **current_release()),
    ), keep=("matches",))
