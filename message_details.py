"""
message_details.py — the iso20022_message_details tool.

Answers "what is this ISO 20022 message?" for one message: its name, AI-generated description,
business area, latest version, and the size and shape of its structure (counts only; the
element tree itself is not returned). Reads i22_message_type, i22_business_area,
i22_message_structure (element counts by depth) and i22_codeset_message (code set count).

The standard variant (variant 1) is described, at the latest version held.

Release checklist references are in square brackets, e.g. [1.2].
"""
import re
from typing import Annotated

from pydantic import BaseModel, Field

from mcp.server.mcpserver.exceptions import ToolError

from mappings import _retired_note          # same wording as the mappings tool for a retired message [7.3]
from shared import Provenance, current_release, query, respond

TOOL_NAME = "iso20022_message_details"

MAX_CLOSEST_IDS = 5

COUNTING_RULE = ("Structure figures count every element declared in the message definition. Optional elements and "
                 "every alternative of a choice are counted alongside mandatory ones, so the figures describe the size "
                 "of the definition, and a single message instance uses a subset of it.")

MESSAGE_SCOPE = ("ISO 20022 message catalogue and parsed message definitions held in the Navigator: the standard "
                 "variant (variant 1) at the latest version held. Structure figures are element counts from the "
                 "message definition; the element tree itself is not returned. " + COUNTING_RULE)

ACCEPTED_FORMS = ("Accepted forms: pacs.008, camt.053 (four letters and three digits; any case; a dot, hyphen, "
                  "underscore, space or no separator), or a full identifier such as pacs.008.001.13.")

# One shape for the SDK's input check [2.1] and for the reader below. Optional parts: variant (3 digits)
# and version (2 digits), so 'pacs.008', 'pacs.008.001' and 'pacs.008.001.13' are all accepted.
SCHEMA_PATTERN = r"^ *[A-Za-z]{4}[ ._\-]?[0-9]{3}([ ._\-][0-9]{3}([ ._\-][0-9]{2})?)? *$"
_ID = re.compile(r"^([a-z]{4})[ ._\-]?([0-9]{3})(?:[ ._\-]([0-9]{3})(?:[ ._\-]([0-9]{2}))?)?$")


# --------------------------------------------------------------------------
# Output models [3.1] — only fields declared here can leave the server.
# --------------------------------------------------------------------------
class BusinessArea(BaseModel):
    code: str = Field(description="Business area code, e.g. camt")
    name: str = Field(description="Business area name, e.g. Cash Management")
    domain: str = Field(description="Product domain the business area belongs to, e.g. Payments")


class Structure(BaseModel):
    total_elements: int = Field(
        description="All elements declared in the message definition, including the document wrapper at depths 0 and 1. "
                    "Optional elements and every alternative of a choice are counted.")
    max_depth: int = Field(description="Deepest nesting level in the message definition")
    elements_by_depth: dict[str, int] | None = Field(
        None, description="Element counts keyed by depth, counted as total_elements is (optional elements and all "
                          "alternatives included). Depth 2 holds the message's top-level blocks; depths 0 and 1 are "
                          "the document wrapper. Runs from depth 2 to max_depth.")


class MessageDetailsResult(BaseModel):
    found: bool
    input: str = Field(description="The message identifier as interpreted by the server, e.g. pacs.008")
    name: str | None = Field(None, description="Official message name, e.g. BankToCustomerAccountReport")
    latest_version: str | None = Field(
        None, description="Full identifier of the latest version held, e.g. camt.053.001.14")
    business_area: BusinessArea | None = None
    navigator_group: str | None = Field(
        None, description="The Navigator's own functional grouping of messages. It is not part of the "
                          "ISO 20022 standard.")
    description: str | None = Field(None, description="AI-generated plain-English description of the message.")
    deactivated_in: str | None = Field(
        None, description="Navigator data load (quarter) in which this message was marked deactivated; "
                          "absent if current")
    structure: Structure | None = Field(
        None, description="Size and shape of the message definition; absent when no structure is held")
    code_set_count: int | None = Field(
        None, description="Number of external code sets the message uses")
    closest_ids: list[str] = Field(
        default_factory=list,
        description="Not found only: up to 5 held messages in the same business area, nearest number first")
    notes: list[str] = Field(default_factory=list, description="Caveats to pass on to the user")
    provenance: Provenance


# --------------------------------------------------------------------------
# SQL [1.1][1.2][1.3] — fixed strings, named placeholders, explicit columns.
# The business area is a LEFT JOIN: a missing area must not make a real message look not-found.
# --------------------------------------------------------------------------
SQL_MESSAGE = """
SELECT m.version, m.message_type, m.message_name, m.message_group, m.rich_description,
       m.deactivated_in,
       b.code      AS area_code,
       b.full_name AS area_name,
       b.domain    AS area_domain
FROM   i22_message_type m
LEFT JOIN i22_business_area b ON b.code = left(m.message_id, 4)
WHERE  m.message_id = %(m)s AND m.variant = 1
"""

# One row per depth; the app sums them. Uses the index on (message_id, variant).
SQL_DEPTHS = """
SELECT depth, count(*) AS n
FROM   i22_message_structure
WHERE  message_id = %(m)s AND variant = 1
GROUP  BY depth
ORDER  BY depth
"""

# The junction table has no versioning columns; its primary key (concept, message_id, variant) makes this a code set count.
SQL_CODESETS = """
SELECT count(*) AS n
FROM   i22_codeset_message
WHERE  message_id = %(m)s AND variant = 1
"""

# Held messages in the same business area, nearest number first.
SQL_CLOSEST = r"""
SELECT message_id, count(*) OVER () AS total
FROM   i22_message_type
WHERE  variant = 1
AND    left(message_id, 4) = %(area)s
AND    message_id ~ '^[a-z]{4}\.[0-9]{3}$'
ORDER  BY abs(split_part(message_id, '.', 2)::int - %(n)s), message_id
LIMIT  %(limit)s
"""


# --------------------------------------------------------------------------
# Input reading
# --------------------------------------------------------------------------
def _parse(message_id: str) -> tuple[str, str, int | None, int | None]:
    """Return (area, number, variant, version). Normalises case, whitespace and separators [2.3]."""
    m = _ID.match(" ".join(message_id.lower().split()))
    if not m:
        raise ToolError(f"'{message_id}' is not an ISO 20022 message identifier. {ACCEPTED_FORMS}")
    area, number, variant, version = m.groups()
    return area, number, int(variant) if variant else None, int(version) if version else None


def _structure(rows: list[dict]) -> Structure | None:
    """Counts by depth from the database rows; None when the message has no structure."""
    if not rows:
        return None
    by_depth = {int(r["depth"]): int(r["n"]) for r in rows}
    shown = {str(d): n for d, n in sorted(by_depth.items()) if d >= 2}
    return Structure(total_elements=sum(by_depth.values()), max_depth=max(by_depth),
                     elements_by_depth=shown or None)


# --------------------------------------------------------------------------
# The tool
# --------------------------------------------------------------------------
def get_message_details(
    message_id: Annotated[
        str,
        Field(
            description=(
                "An ISO 20022 message identifier: four letters and three digits, e.g. 'pacs.008' or 'camt.053'. "
                "Any case works, and a hyphen, underscore, space or no separator also works ('pacs-008', "
                "'pacs008'). A full identifier with variant and version ('pacs.008.001.13') is accepted; the "
                "response gives the latest version held and notes any difference."
            ),
            min_length=7,
            max_length=24,
            pattern=SCHEMA_PATTERN,
        ),
    ],
) -> MessageDetailsResult:
    """Use for ANY question about what an ISO 20022 message is: its official name, purpose, business
    area, latest version, and the size and shape of its structure. Send a message identifier such
    as pacs.008, camt.053 or pain.001. Call it even when the message looks familiar: it returns the
    official name, a plain-English description, the business area and domain, the Navigator's own
    functional grouping, whether the message is retired, structure statistics (total elements,
    maximum depth, element counts at each depth) and the number of external code sets the message uses.

    Covers the standard variant (variant 1) of each message, at the latest version held. For legacy
    equivalents (SWIFT MT, NACHA, CHAPS) and the schemes that use a message, use
    iso20022_legacy_and_scheme_mappings.
    """
    area, number, req_variant, req_version = _parse(message_id)
    short = f"{area}.{number}"
    notes: list[str] = []

    rows = query(TOOL_NAME, SQL_MESSAGE, {"m": short})
    if not rows:
        return _not_found(short, area, number, notes)
    row = rows[0]

    # Only an explicit full identifier can differ from what is held [2.3].
    if req_variant not in (None, 1):
        notes.append(f"Variant {req_variant:03d} of {short} was requested; this tool covers the standard "
                     "variant (variant 1), which is shown here.")
    elif req_version is not None and req_version != row["version"]:
        notes.append(f"Version {req_version:02d} of {short} is not held; this response describes "
                     f"{row['message_type']}, the latest version held.")

    if row["deactivated_in"]:
        notes.append(_retired_note(short, row["message_name"], row["deactivated_in"]))

    structure = _structure(query(TOOL_NAME, SQL_DEPTHS, {"m": short}))
    if structure is None:
        notes.append(f"No structure is held for {short}, so structure statistics are not available.")
    code_sets = query(TOOL_NAME, SQL_CODESETS, {"m": short})[0]["n"]

    return respond(MessageDetailsResult(
        found=True, input=short, name=row["message_name"], latest_version=row["message_type"],
        business_area=(BusinessArea(code=row["area_code"], name=row["area_name"], domain=row["area_domain"])
                       if row["area_code"] else None),
        navigator_group=row["message_group"], description=row["rich_description"],
        deactivated_in=row["deactivated_in"], structure=structure, code_set_count=code_sets,
        notes=notes, provenance=Provenance(scope=MESSAGE_SCOPE, **current_release()),
    ))


def _not_found(short: str, area: str, number: str, notes: list[str]) -> object:
    """Explicit, explained not-found with the nearest held messages in the same area [3.5][7.1]."""
    near = query(TOOL_NAME, SQL_CLOSEST, {"area": area, "n": int(number), "limit": MAX_CLOSEST_IDS})
    notes.append(f"{short} is not in this dataset. The dataset holds most but not all ISO 20022 messages, "
                 "so a message missing here may still exist at ISO. Do not describe it from general knowledge.")
    if near:
        if near[0]["total"] > len(near):
            notes.append(f"Showing {len(near)} of {near[0]['total']} held messages in the {area} business area, "
                         "nearest number first.")
    else:
        notes.append(f"No messages are held for a business area with the code '{area}'.")
    return respond(MessageDetailsResult(
        found=False, input=short, closest_ids=[r["message_id"] for r in near], notes=notes,
        provenance=Provenance(scope=MESSAGE_SCOPE, **current_release()),
    ))
