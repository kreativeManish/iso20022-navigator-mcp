"""
access_snapshot.py — list what the iso20022_mcp role can read, for review (checklist 8.2 / 8.5).

    python tools/access_snapshot.py

Prints every readable schema.table.column and the SHA-256 of that list. Review the
list privately; if it is approved, save the hash to tests/access_snapshot.sha256.
Do not commit the list itself: the repository is public.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

import support  # noqa: E402

if not support.HAS_DB:
    sys.exit("No DATABASE_URL found (.env or environment).")

support.db.pool.open(wait=True, timeout=30)
try:
    lines, digest = support.access_snapshot()
finally:
    support.db.pool.close()

print("\n".join(lines))
print(f"\n{len(lines)} readable columns")
print(f"SHA-256: {digest}")
