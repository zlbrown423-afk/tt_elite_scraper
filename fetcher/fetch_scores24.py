#!/usr/bin/env python3
"""
fetch_scores24.py -- production fetch layer for Scores24 TT Elite data.

Calls the verified `leaguesMatches` rapi endpoint directly (no full-page
HTML fetch, no Playwright) and paginates it with the API's own cursor,
using ONLY parameter names/values confirmed against the live endpoint by
the `probe/verify_pagination.py` GitHub Actions run:

    GET https://scores24.live/rapi/leagues/table-tennis/{leagueSlug}/matches
        ?lang=en&first=<n>&status=<live|not_started|ended>
        &date_between[]=<from>&date_between[]=<to>
        &with_statistics=false&audience=en
        &after=<cursor from the previous page's pageInfo.endCursor>   (page 2+)

This module does the network I/O and pagination ONLY. Every match node it
receives is handed to `parser.parse_scores24.parse_match_node` (imported,
not reimplemented) for the actual field mapping, name normalization, and
finished/live logic -- that code was already reviewed against real data and
is reused as-is here, per instruction not to duplicate it. Cross-page /
cross-status dedupe and conflict detection also reuse
`parser.parse_scores24.merge_and_dedupe` unchanged.

What is genuinely new in this file:
  - HTTP calls with retries/timeouts (parser.parse_scores24 has none --
    it only ever read local files).
  - Cursor-based pagination, driven ENTIRELY by each response's own
    `pageInfo.hasNextPage` / `pageInfo.endCursor` -- no cursor is ever
    hard-coded, guessed, or carried over from a previous run.
  - Saving the raw JSON of every page fetched, for reproducibility.
  - A verification report (page/record/dedupe/conflict counts) so a human
    can sanity-check a run without reading the full dataset.

Explicitly NOT done here (unchanged from parser/parse_scores24.py's scope):
  - No writes into the TT Elite Terminal's database.
  - No prediction-model, weighting, threshold, or betting-logic code.
  - No cross-source (Scores24-vs-existing-roster) identity matching.

Usage:
    python fetcher/fetch_scores24.py \
        --league-slug tt-elite-series-1 \
        --status ended --status live --status not_started \
        --first 25 --max-pages 20 \
        --raw-dir fetcher/output/raw \
        --out fetcher/output/fetched.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "parser"))
from parse_scores24 import (  # noqa: E402  (path insert must come first)
    ACTIVE_LEAGUE_SLUGS,
    merge_and_dedupe,
    parse_match_node,
)

RAPI_URL_TEMPLATE = "https://scores24.live/rapi/leagues/table-tennis/{league_slug}/matches"

COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://scores24.live/en/table-tennis/l-tt-elite-series-1",
}

# Default date_between[] windows, mirroring exactly what the live Scores24
# frontend itself requests for each status bucket (observed in the probe's
# window.__REACT_QUERY_STATE__ queryKeys) -- NOT invented. All are
# overridable via --date-from/--date-to for backfill use.
def _default_date_between(status: str, now_utc: datetime) -> list[str]:
    today = now_utc.strftime("%Y-%m-%d 00:00:00")
    if status == "ended":
        return ["", today]
    if status == "not_started":
        return [today, ""]
    if status == "live":
        one_day_ago = now_utc.strftime("%Y-%m-%d 12:00:00")  # coarse window; live matches don't need pagination depth
        return [one_day_ago, today]
    raise ValueError(f"no default date window for status={status!r}")


class FetchError(Exception):
    """Raised when a request ultimately fails, or the response doesn't have
    the shape we need to continue safely. Always carries enough detail
    (status code / body snippet) to diagnose without re-running blind."""


@dataclass
class PageResult:
    status: str
    page_index: int
    url: str
    retrieved_at_utc: str
    raw_path: Optional[str]
    edges: list[dict]
    page_info: dict


def _request_with_retries(
    session: requests.Session,
    url: str,
    params: dict,
    *,
    timeout: float,
    max_retries: int,
    backoff_base: float,
) -> requests.Response:
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, params=params, headers=COMMON_HEADERS, timeout=timeout)
        except requests.RequestException as e:
            last_exc = e
            if attempt < max_retries:
                time.sleep(backoff_base * (2 ** (attempt - 1)))
                continue
            raise FetchError(
                f"request failed after {max_retries} attempts: {type(e).__name__}: {e}"
            ) from e

        if resp.status_code == 200:
            return resp
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
            time.sleep(backoff_base * (2 ** (attempt - 1)))
            continue
        raise FetchError(
            f"non-200 response ({resp.status_code}) from {resp.url!r} on attempt "
            f"{attempt}/{max_retries}. Body (first 1000 chars): {resp.text[:1000]!r}"
        )
    # unreachable, but keeps type checkers happy
    raise FetchError(f"exhausted retries: {last_exc}")


def _extract_edges_and_page_info(raw_json: Any, *, response_url: str) -> tuple[list[dict], dict]:
    """
    The exact top-level shape of a direct rapi call was NOT captured in the
    probe (the probe only confirmed status 200 + 'valid JSON with edges +
    pageInfo', not the precise nesting). Support both shapes actually seen
    in this project -- the hydration-payload shape
    ({"data": {"edges": [...], "pageInfo": {...}}}) and a plausible flat
    shape ({"edges": [...], "pageInfo": {...}}) -- and FAIL LOUDLY with the
    real body if neither matches, rather than guessing a third shape.
    """
    if isinstance(raw_json, dict):
        inner = raw_json.get("data")
        if isinstance(inner, dict) and "edges" in inner and "pageInfo" in inner:
            return inner["edges"] or [], inner["pageInfo"] or {}
        if "edges" in raw_json and "pageInfo" in raw_json:
            return raw_json["edges"] or [], raw_json["pageInfo"] or {}
    raise FetchError(
        f"response from {response_url!r} did not match either known shape "
        f"({{data:{{edges,pageInfo}}}} or {{edges,pageInfo}}). Top-level keys: "
        f"{list(raw_json.keys()) if isinstance(raw_json, dict) else type(raw_json).__name__}. "
        f"This must be resolved by inspecting the actual response, not by guessing -- "
        f"see the raw dump for this page."
    )


def fetch_all_pages(
    session: requests.Session,
    *,
    league_slug: str,
    status: str,
    first: int,
    date_between: list[str],
    max_pages: int,
    timeout: float,
    max_retries: int,
    backoff_base: float,
    raw_dir: Optional[str],
) -> tuple[list[PageResult], bool, Optional[str]]:
    """
    Returns (pages, terminated_normally, error_message).

    terminated_normally is True only if pagination stopped because the API
    itself returned hasNextPage=false. If the max_pages safety cap is hit
    first, terminated_normally is False and error_message explains why --
    this is not silently treated as "done".
    """
    url = RAPI_URL_TEMPLATE.format(league_slug=league_slug)
    pages: list[PageResult] = []
    cursor: Optional[str] = None
    page_index = 0

    while True:
        params = {
            "lang": "en",
            "first": first,
            "status": status,
            "date_between[]": date_between,
            "with_statistics": "false",
            "audience": "en",
        }
        if cursor is not None:
            params["after"] = cursor

        try:
            resp = _request_with_retries(
                session, url, params,
                timeout=timeout, max_retries=max_retries, backoff_base=backoff_base,
            )
            raw_json = resp.json()
            edges, page_info = _extract_edges_and_page_info(raw_json, response_url=resp.url)
        except (FetchError, ValueError) as e:
            return pages, False, f"status={status!r} page={page_index}: {e}"

        retrieved_at = datetime.now(timezone.utc).isoformat()
        raw_path = None
        if raw_dir:
            os.makedirs(raw_dir, exist_ok=True)
            raw_path = os.path.join(raw_dir, f"{league_slug}_{status}_page{page_index}.json")
            with open(raw_path, "w", encoding="utf-8") as f:
                json.dump(raw_json, f, indent=2, ensure_ascii=False)

        pages.append(
            PageResult(
                status=status,
                page_index=page_index,
                url=resp.url,
                retrieved_at_utc=retrieved_at,
                raw_path=raw_path,
                edges=edges,
                page_info=page_info,
            )
        )

        page_index += 1
        if page_index >= max_pages:
            return pages, False, (
                f"status={status!r}: hit --max-pages cap ({max_pages}) before "
                f"hasNextPage=false -- pagination was NOT exhausted, this dataset "
                f"is a partial window, not the full history."
            )

        has_next = page_info.get("hasNextPage")
        if has_next is None:
            return pages, False, (
                f"status={status!r} page={page_index - 1}: response pageInfo had "
                f"no 'hasNextPage' key -- cannot determine whether more pages "
                f"exist without guessing. pageInfo keys: {list(page_info.keys())}"
            )
        if not has_next:
            return pages, True, None

        end_cursor = page_info.get("endCursor")
        if not end_cursor:
            return pages, False, (
                f"status={status!r} page={page_index - 1}: hasNextPage=true but "
                f"pageInfo.endCursor is missing/empty -- cannot continue "
                f"pagination without a cursor to use, and one is not fabricated here."
            )
        cursor = end_cursor


def run_fetch(
    *,
    league_slug: str,
    statuses: list[str],
    first: int,
    date_from: Optional[str],
    date_to: Optional[str],
    max_pages: int,
    timeout: float,
    max_retries: int,
    backoff_base: float,
    raw_dir: Optional[str],
) -> dict:
    session = requests.Session()
    now_utc = datetime.now(timezone.utc)

    all_raw_records: list[dict] = []
    per_status_report = []
    any_failure = False

    for status in statuses:
        if date_from is not None or date_to is not None:
            date_between = [date_from or "", date_to or ""]
        else:
            date_between = _default_date_between(status, now_utc)

        pages, terminated_normally, error_message = fetch_all_pages(
            session,
            league_slug=league_slug,
            status=status,
            first=first,
            date_between=date_between,
            max_pages=max_pages,
            timeout=timeout,
            max_retries=max_retries,
            backoff_base=backoff_base,
            raw_dir=raw_dir,
        )

        raw_node_count = 0
        for page in pages:
            for edge in page.edges:
                node = edge.get("node")
                if not isinstance(node, dict):
                    continue
                raw_node_count += 1
                record, notes = parse_match_node(
                    node,
                    status=status,
                    source_league_slug=league_slug,
                    source_url=page.url,
                    source_file=page.raw_path or f"<not saved: {league_slug}_{status}_page{page.page_index}>",
                    retrieved_at_utc=page.retrieved_at_utc,
                    fetch_method="api",
                )
                record["_parseNotes"] = notes  # consumed below, stripped before final output
                all_raw_records.append(record)

        if error_message:
            any_failure = True

        per_status_report.append(
            {
                "status": status,
                "leagueSlug": league_slug,
                "dateBetween": date_between,
                "pagesFetched": len(pages),
                "rawNodeCount": raw_node_count,
                "terminatedNormally": terminated_normally,
                "error": error_message,
            }
        )

    # Pull per-record parse notes into the review queue before handing off
    # to the shared merge/dedupe (which itself only inspects the schema
    # fields, not this scratch field).
    parse_note_items = []
    for r in all_raw_records:
        notes = r.pop("_parseNotes", [])
        if notes:
            parse_note_items.append(
                {"type": "PARSE_NOTE", "matchId": r.get("matchId"), "sourceFile": r.get("sourceFile"), "notes": notes}
            )

    merged, dupe_log, status_conflicts = merge_and_dedupe(all_raw_records)
    merged.sort(key=lambda r: (r.get("dateISO") or "", r.get("matchId") or ""))

    review_queue = parse_note_items + status_conflicts

    dates = [r["dateISO"] for r in merged if r.get("dateISO")]
    first_ts = min(dates) if dates else None
    last_ts = max(dates) if dates else None

    report = {
        "runMeta": {
            "fetchedAtUTC": now_utc.isoformat(),
            "leagueSlug": league_slug,
            "isActiveLeague": league_slug in ACTIVE_LEAGUE_SLUGS,
            "statusesRequested": statuses,
            "firstParam": first,
            "maxPagesCap": max_pages,
            "success": not any_failure,
        },
        "perStatus": per_status_report,
        "records": merged,
        "reviewQueue": review_queue,
        "dedupeLog": dupe_log,
        "verification": {
            "apiPagesFetched": sum(p["pagesFetched"] for p in per_status_report),
            "rawMatchNodesReceived": sum(p["rawNodeCount"] for p in per_status_report),
            "uniqueMatchIds": len(merged),
            "duplicateIdsCollapsed": len(dupe_log),
            "statusConflicts": len(status_conflicts),
            "finishedRecords": sum(1 for r in merged if r.get("isFinished")),
            "liveRecords": sum(1 for r in merged if r.get("isLive")),
            "notStartedRecords": sum(1 for r in merged if not r.get("isFinished") and not r.get("isLive")),
            "firstMatchTimestamp": first_ts,
            "lastMatchTimestamp": last_ts,
            "paginationTerminatedNormally": {p["status"]: p["terminatedNormally"] for p in per_status_report},
            "anyFailure": any_failure,
        },
    }
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--league-slug", default="tt-elite-series-1")
    ap.add_argument(
        "--status", dest="statuses", action="append", choices=["live", "not_started", "ended"],
        help="repeatable; defaults to all three if omitted",
    )
    ap.add_argument("--first", type=int, default=25, help="page size (the 'first' param)")
    ap.add_argument("--date-from", default=None, help="overrides the default date_between[0] for ALL requested statuses")
    ap.add_argument("--date-to", default=None, help="overrides the default date_between[1] for ALL requested statuses")
    ap.add_argument("--max-pages", type=int, default=20, help="safety cap on pages PER status -- pagination is flagged non-normal if this is hit")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--max-retries", type=int, default=3)
    ap.add_argument("--backoff-base", type=float, default=1.5)
    ap.add_argument("--raw-dir", default=None, help="directory to save every raw API page response; omit to skip saving raw pages")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    statuses = args.statuses or ["live", "not_started", "ended"]

    report = run_fetch(
        league_slug=args.league_slug,
        statuses=statuses,
        first=args.first,
        date_from=args.date_from,
        date_to=args.date_to,
        max_pages=args.max_pages,
        timeout=args.timeout,
        max_retries=args.max_retries,
        backoff_base=args.backoff_base,
        raw_dir=args.raw_dir,
    )

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    v = report["verification"]
    print(
        f"success={report['runMeta']['success']} "
        f"pages={v['apiPagesFetched']} rawNodes={v['rawMatchNodesReceived']} "
        f"unique={v['uniqueMatchIds']} dupes={v['duplicateIdsCollapsed']} "
        f"conflicts={v['statusConflicts']} "
        f"finished={v['finishedRecords']} live={v['liveRecords']} notStarted={v['notStartedRecords']} "
        f"range=[{v['firstMatchTimestamp']}..{v['lastMatchTimestamp']}] "
        f"terminatedNormally={v['paginationTerminatedNormally']} "
        f"-> {args.out}",
        file=sys.stderr,
    )

    if not report["runMeta"]["success"]:
        for s in report["perStatus"]:
            if s["error"]:
                print(f"FAILURE for status={s['status']!r}: {s['error']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
