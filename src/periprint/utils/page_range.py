from __future__ import annotations

import re

# 1-based per user-facing convention ("2-4,7" means pages 2,3,4,7 as a
# human counts them), converted to 0-based indices for internal use.
_TOKEN_RE = re.compile(r"^(\d+)(-(\d+))?$")


def parse_page_range(range_str: str, total_pages: int) -> list[int]:
    """periprint-spec.md §3 P1: "2-4,7" style page selection. Empty string
    means "all pages". Returns 0-based indices in first-occurrence order,
    duplicates removed, clamped to [0, total_pages) — a token naming a
    page beyond total_pages (e.g. "2-100" on a 5-page doc) is silently
    trimmed rather than rejected, since not knowing a document's exact
    page count is a normal, forgivable user mistake. Malformed syntax
    (not digits/a "start-end" range) raises ValueError instead — that's
    a typo worth surfacing, not silently swallowing."""
    range_str = range_str.strip()
    if not range_str:
        return list(range(total_pages))

    indices: list[int] = []
    seen: set[int] = set()
    for raw_token in range_str.split(","):
        token = raw_token.replace(" ", "").strip()
        if not token:
            continue
        match = _TOKEN_RE.match(token)
        if not match:
            raise ValueError(f"Invalid page range token: {token!r}")
        start = int(match.group(1))
        end = int(match.group(3)) if match.group(3) else start
        if start < 1 or end < start:
            raise ValueError(f"Invalid page range token: {token!r}")
        for page in range(start, end + 1):
            index = page - 1
            if index < total_pages and index not in seen:
                seen.add(index)
                indices.append(index)
    return indices
