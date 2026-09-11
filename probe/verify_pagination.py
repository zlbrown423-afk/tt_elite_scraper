#!/usr/bin/env python3
"""
verify_pagination.py -- PROBE, not production code.

Purpose: find out whether the `leaguesMatches` rapi endpoint that the
Scores24 frontend calls (as seen in `window.__REACT_QUERY_STATE__`'s
queryKey) can actually be called directly, and if so, what request shape
and cursor parameter it expects for pagination.

This does NOT assume the answer. The production parser (parser/parse_scores24.py)
does not call this endpoint at all yet -- it only reads saved HTML files.
Wiring real pagination into the production fetch step must wait until this
probe has been run (from GitHub Actions, where network egress actually
works) and its output has been reviewed.

What it tries, and records the raw result of each (status code, content-type,
first ~2KB of body, and whether the body parses as JSON with an 'edges'/'pageInfo'
shape) without editorializing on success:

  1. A direct GET to https://scores24.live/rapi/... built from the exact
     path/query parameters observed in queryKey[0] for the 'ended' query
     (first=5, status=ended, date_between[]=[...]), with NO cursor -- to
     see if the base call works at all outside the page-hydration context
     (it may require specific headers, e.g. a Referer or an API key, that
     aren't visible in the static HTML).
  2. The same call with a plausible Relay-style `after` cursor param set to
     the real endCursor decoded from the probe's saved HTML
     (20260910234500_ts_4wyrn0t5el7vm86 for the 'ended' bucket), to see
     whether that specific parameter name/format is accepted.
  3. The same call requesting a larger page (`first=50` instead of 5), to
     see whether page size is respected or capped server-side.

Each attempt is logged independently. No attempt result is used to
overwrite or assume the others succeeded.

Usage (from GitHub Actions, where network access is unrestricted):
    python probe/verify_pagination.py --out probe/output/pagination_probe.json
"""

from __future__ import annotations

import argparse
import base64
import json
import sys

import requests

BASE_URL = "https://scores24.live/rapi/leagues/table-tennis/tt-elite-series-1/matches"
# NOTE: the exact rapi URL shape (path segments vs query params) is inferred
# from queryKey[0]={'baseUrl','path','query'} seen in the probe HTML, but the
# precise way scores24's frontend router turns {path, query} into a URL was
# NOT observed directly (no network trace was captured, only the post-hoc
# cache). This script tries the most likely shape and reports what actually
# comes back -- it does not assume this URL is correct.

COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://scores24.live/en/table-tennis/l-tt-elite-series-1",
}

ENDED_QUERY_PARAMS = {
    "lang": "en",
    "first": 5,
    "status": "ended",
    "date_between[]": ["", "2026-09-11 00:00:00"],
    "with_statistics": "false",
    "audience": "en",
}

KNOWN_END_CURSOR_B64 = "MjAyNjA5MTAyMzQ1MDBfdHNfNHd5cm4wdDVlbDd2bTg2"
KNOWN_END_CURSOR_DECODED = base64.b64decode(KNOWN_END_CURSOR_B64).decode()


def _summarize_response(resp: requests.Response) -> dict:
    body_preview = resp.text[:2000]
    parsed_shape = None
    try:
        j = resp.json()
        if isinstance(j, dict):
            parsed_shape = {
                "topLevelKeys": list(j.keys())[:20],
                "hasEdges": "edges" in j or ("data" in j and isinstance(j.get("data"), dict) and "edges" in j["data"]),
                "hasPageInfo": "pageInfo" in j or ("data" in j and isinstance(j.get("data"), dict) and "pageInfo" in j["data"]),
            }
    except Exception:
        pass
    return {
        "statusCode": resp.status_code,
        "contentType": resp.headers.get("content-type"),
        "bodyPreview": body_preview,
        "parsedJsonShape": parsed_shape,
        "finalUrl": resp.url,
    }


def attempt(label: str, params: dict) -> dict:
    result = {"label": label, "requestedUrl": BASE_URL, "requestedParams": params}
    try:
        resp = requests.get(BASE_URL, params=params, headers=COMMON_HEADERS, timeout=15)
        result["response"] = _summarize_response(resp)
        result["ok"] = True
    except Exception as e:
        result["ok"] = False
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    results = []

    results.append(attempt("base_call_no_cursor", dict(ENDED_QUERY_PARAMS)))

    with_cursor = dict(ENDED_QUERY_PARAMS)
    with_cursor["after"] = KNOWN_END_CURSOR_B64
    results.append(attempt("with_after_cursor_b64", with_cursor))

    larger_page = dict(ENDED_QUERY_PARAMS)
    larger_page["first"] = 50
    results.append(attempt("larger_page_size", larger_page))

    out = {
        "purpose": "diagnostic probe of leaguesMatches direct-call and pagination behavior -- not proof of a working integration",
        "knownEndCursorDecoded": KNOWN_END_CURSOR_DECODED,
        "attempts": results,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print(f"Wrote {len(results)} probe attempts -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
