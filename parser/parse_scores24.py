#!/usr/bin/env python3
"""
parse_scores24.py

Deterministic parser for Scores24 table-tennis league pages.

Scope (per project decision after the probe phase):
  - Reads the `window.__REACT_QUERY_STATE__` hydration payload embedded in a
    saved Scores24 league page (plain HTTP fetch -- Playwright is NOT used in
    production; see PROBE_FINDINGS.md).
  - Extracts the three `leaguesMatches` queries (status=live / not_started /
    ended) and maps each match node to a normalized record.
  - Uses the Scores24-internal match `id` as the dedup key, and each player's
    Scores24 `id`/`slug` as their identity key -- NOT their display name.
  - Does NOT guess. Anything that can't be mapped deterministically is
    written to the `notes` / `reviewQueue` lists in the output instead of
    being silently resolved.

Explicitly OUT OF SCOPE for this module (left for the later "connect to the
Terminal" step, per instruction):
  - Matching a Scores24 player identity against the existing dashboard
    player roster (that roster has no Scores24 ids to match against yet).
  - Any write into the TT Elite Terminal's own database.
  - Any prediction-model, weighting, or betting-logic code.

Usage:
    python parse_scores24.py \
        --summary probe/output/summary.json \
        --html probe/output/scores24_tournament_1_requests.html \
        --html probe/output/scores24_tournament_2_requests.html \
        --out parser/output/parsed.json

Each --html file is matched against the run's summary.json by filename to
recover its source URL, fetch method, and retrieval timestamp -- these are
never fabricated or guessed; if a file can't be matched, the record set from
that file is still parsed but flagged with sourceMetadataMissing=true rather
than inventing a URL or timestamp.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

REACT_QUERY_STATE_MARKER = "window.__REACT_QUERY_STATE__=JSON.parse("
LEAGUES_MATCHES_ID = "leaguesMatches"

# Leagues this project currently treats as the *active* dataset. Anything
# else (e.g. tt-elite-series-2, confirmed stale/archival as of the probe --
# most recent match 2025-03-12, zero not_started matches) is parsed and
# preserved, but tagged isActiveLeague=False and excluded from any "active"
# view unless a future run is explicitly configured otherwise.
ACTIVE_LEAGUE_SLUGS = {"tt-elite-series-1"}


# --------------------------------------------------------------------------
# Stage 1: pull the hydration payload out of the raw HTML
# --------------------------------------------------------------------------

class ExtractionError(Exception):
    pass


def _scan_js_string_literal(html: str, start_quote_idx: int) -> str:
    """
    Return the full JS double-quoted string literal (including both quote
    characters) starting at `start_quote_idx`, which must point at the
    opening '"'. Walks character-by-character treating a backslash as
    "escape, skip the next character" so an escaped quote inside the
    payload doesn't terminate the scan early (a naive non-greedy regex gets
    this wrong on real Scores24 payloads -- confirmed during the probe
    investigation).
    """
    if html[start_quote_idx] != '"':
        raise ExtractionError(
            f"expected '\"' at offset {start_quote_idx}, found {html[start_quote_idx]!r}"
        )
    i = start_quote_idx + 1
    n = len(html)
    while i < n:
        c = html[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return html[start_quote_idx : i + 1]
        i += 1
    raise ExtractionError("unterminated string literal (reached end of file)")


def extract_react_query_state(html: str) -> dict[str, Any]:
    """
    Locate `window.__REACT_QUERY_STATE__=JSON.parse("...")` in the raw HTML
    and return the decoded object. The payload is double-JSON-encoded: the
    JS string literal, once JSON-decoded once, yields ANOTHER JSON string,
    which must be decoded a second time to reach the real object.
    """
    idx = html.find(REACT_QUERY_STATE_MARKER)
    if idx == -1:
        raise ExtractionError(
            "window.__REACT_QUERY_STATE__ marker not found in this file -- "
            "page structure may have changed since the probe; do not guess, "
            "escalate for re-probing."
        )
    literal_start = idx + len(REACT_QUERY_STATE_MARKER)
    literal = _scan_js_string_literal(html, literal_start)

    try:
        stage1 = json.loads(literal)
    except json.JSONDecodeError as e:
        raise ExtractionError(f"stage-1 JSON decode failed: {e}") from e
    if not isinstance(stage1, str):
        raise ExtractionError(
            f"expected stage-1 decode to yield a string (double-encoded "
            f"payload), got {type(stage1).__name__} -- payload format may "
            f"have changed."
        )
    try:
        stage2 = json.loads(stage1)
    except json.JSONDecodeError as e:
        raise ExtractionError(f"stage-2 JSON decode failed: {e}") from e
    if not isinstance(stage2, dict) or "queries" not in stage2:
        raise ExtractionError(
            "decoded payload does not have the expected {'queries': [...]} "
            "shape -- payload format may have changed."
        )
    return stage2


# --------------------------------------------------------------------------
# Stage 2: pull out the leaguesMatches queries
# --------------------------------------------------------------------------

@dataclass
class MatchesQuery:
    status: str  # 'live' | 'not_started' | 'ended'
    league_slug: Optional[str]
    edges: list[dict]
    page_info: Optional[dict]
    raw_query_params: dict


def iter_leagues_matches_queries(state: dict[str, Any]):
    for q in state.get("queries", []):
        key0 = (q.get("queryKey") or [None])[0]
        if not isinstance(key0, dict) or key0.get("_id") != LEAGUES_MATCHES_ID:
            continue
        query_params = key0.get("query") or {}
        status = query_params.get("status")
        league_slug = (key0.get("path") or {}).get("leagueSlug")

        data = ((q.get("state") or {}).get("data") or {}).get("data")
        if not isinstance(data, dict):
            # A query that hasn't resolved / has no data -- skip, don't guess.
            continue
        edges = data.get("edges") or []
        page_info = data.get("pageInfo")
        yield MatchesQuery(
            status=status,
            league_slug=league_slug,
            edges=edges,
            page_info=page_info,
            raw_query_params=query_params,
        )


# --------------------------------------------------------------------------
# Stage 3: display-name normalization (cosmetic only -- identity is never
# derived from this; identity is always the Scores24 id/slug)
# --------------------------------------------------------------------------

# Matches "Last, First" or "Last, First Middle" -- exactly one comma,
# non-empty text on both sides. Confirmed present in real probe data (e.g.
# team.name == "Durak, Kamil" alongside a sibling team.name == "Marian
# Lebek" in the SAME response -- Scores24's own `name` field is
# inconsistently formatted, not something we introduced).
_COMMA_NAME_RE = re.compile(r"^\s*([^,]+?)\s*,\s*(.+?)\s*$")


def normalize_display_name(raw_name: Optional[str]) -> tuple[Optional[str], bool, Optional[str]]:
    """
    Returns (normalized_name, was_changed, anomaly_note).

    Only performs the one deterministic, reversible transform observed in
    real data: "Last, First" -> "First Last" when the string contains
    exactly one comma. Anything else (no comma -> left as-is; more than one
    comma, or empty segments -> left as-is AND flagged as an anomaly note,
    never guessed at).
    """
    if raw_name is None:
        return None, False, "name field missing"
    if raw_name.count(",") == 0:
        return raw_name, False, None
    if raw_name.count(",") > 1:
        return raw_name, False, f"name has multiple commas, left unchanged: {raw_name!r}"
    m = _COMMA_NAME_RE.match(raw_name)
    if not m:
        return raw_name, False, f"comma-formatted name did not match expected pattern: {raw_name!r}"
    last, first = m.group(1), m.group(2)
    if not last or not first:
        return raw_name, False, f"comma-formatted name had an empty segment: {raw_name!r}"
    return f"{first} {last}", True, None


# --------------------------------------------------------------------------
# Stage 4: node -> normalized record
# --------------------------------------------------------------------------

def _player_dict(team_node: dict, notes: list[str]) -> dict:
    raw_name = team_node.get("name")
    normalized, changed, anomaly = normalize_display_name(raw_name)
    if anomaly:
        notes.append(anomaly)
    out = {
        "name": normalized,
        "sourceId": team_node.get("id"),
        "slug": team_node.get("slug"),
    }
    if changed:
        out["nameRaw"] = raw_name
    return out


def parse_match_node(
    node: dict,
    *,
    status: str,
    source_league_slug: Optional[str],
    source_url: Optional[str],
    source_file: str,
    retrieved_at_utc: Optional[str],
    fetch_method: Optional[str],
) -> tuple[dict, list[str]]:
    notes: list[str] = []

    teams = node.get("teams") or []
    if len(teams) != 2:
        notes.append(f"expected exactly 2 teams, found {len(teams)} -- record kept but incomplete")
    player_a = _player_dict(teams[0], notes) if len(teams) > 0 else None
    player_b = _player_dict(teams[1], notes) if len(teams) > 1 else None

    is_finished = node.get("isFinished") is True
    is_live = node.get("isLive") is True
    winner_raw = node.get("winner")

    # Requirement: `resultScore is not None` is NOT itself proof of a
    # completed match (a live match also has a non-null, partial
    # resultScore). isFinished is the authoritative "this match is over"
    # signal. A not_started node carries a placeholder
    # resultScores=[{"type":"FT","value":"0:0"}] even though nothing has
    # happened -- that placeholder must never be read as a real result.
    final_score = None
    set_scores: list[dict] = []
    live_score_snapshot = None

    result_scores = node.get("resultScores") or []
    if is_finished:
        final_score = node.get("resultScore")
        for rs in result_scores:
            t = rs.get("type")
            if t == "FT":
                continue
            set_scores.append({"set": t, "value": rs.get("value")})
        if final_score is None:
            notes.append("isFinished=true but resultScore is null -- unexpected, kept as-is, no value guessed")
    elif is_live:
        # Preserve the in-progress score as a clearly-separate, clearly
        # non-final field rather than discarding it.
        live_score_snapshot = {
            "scoreSoFar": node.get("resultScore"),
            "setsSoFar": [
                {"set": rs.get("type"), "value": rs.get("value")}
                for rs in result_scores
                if rs.get("type") != "FT"
            ],
            "serving": node.get("serving"),
        }
    # else: not started -- final_score stays None, set_scores stays [],
    # the placeholder FT 0:0 is never surfaced anywhere in the output.

    winner_side = None
    if winner_raw == 1:
        winner_side = "A"
    elif winner_raw == 2:
        winner_side = "B"
    elif winner_raw is not None:
        notes.append(f"unexpected winner value {winner_raw!r} (expected 1, 2, or null) -- left as null")
    if winner_side is not None and not is_finished:
        notes.append("winner is set but isFinished is false -- kept, flagged for review")

    if winner_side is not None and final_score is None:
        notes.append("winner is set but no finalScore was recorded -- inconsistent, flagged for review")

    record = {
        "matchId": node.get("id"),
        "source": "scores24",
        "sourceLeagueSlug": source_league_slug or node.get("leagueSlug"),
        "isActiveLeague": (source_league_slug or node.get("leagueSlug")) in ACTIVE_LEAGUE_SLUGS,
        "dateISO": node.get("matchDate"),
        "playerA": player_a,
        "playerB": player_b,
        "status": status,
        "isFinished": is_finished,
        "isLive": is_live,
        "winnerSide": winner_side,
        "finalScore": final_score,
        "setScores": set_scores,
        "liveScoreSnapshot": live_score_snapshot,
        "matchSlug": node.get("slug"),
        "retrievedAtUTC": retrieved_at_utc,
        "sourceUrl": source_url,
        "sourceFile": source_file,
        "fetchMethod": fetch_method,
    }

    if record["matchId"] is None:
        notes.append("match node has no id -- cannot be used as a dedup key, flagged for review")

    return record, notes


# --------------------------------------------------------------------------
# Stage 5: per-file parse + run-level merge/dedupe
# --------------------------------------------------------------------------

@dataclass
class FileMeta:
    source_url: Optional[str] = None
    fetch_method: Optional[str] = None
    retrieved_at_utc: Optional[str] = None
    matched: bool = False


def load_run_metadata(summary_path: str) -> dict[str, FileMeta]:
    """
    Reads the probe's own summary.json and returns a map from the basename
    of each saved HTML file to its {url, method, retrievedAtUTC}. This is
    the ONLY source used for source URL / retrieval timestamp -- nothing is
    inferred from the HTML content or the local filename.
    """
    with open(summary_path, "r", encoding="utf-8") as f:
        summary = json.load(f)

    probed_at = summary.get("probedAtUTC")
    out: dict[str, FileMeta] = {}
    for target_name, target in (summary.get("targets") or {}).items():
        for method in ("requests", "playwright"):
            entry = target.get(method) or {}
            saved_as = entry.get("savedAs")
            if not saved_as or entry.get("status") != 200 and not entry.get("ok", True):
                # playwright entries that failed (e.g. the league-2 timeout)
                # have no savedAs / no file -- nothing to map.
                continue
            if not saved_as:
                continue
            base = os.path.basename(saved_as)
            out[base] = FileMeta(
                source_url=entry.get("final_url") or target.get("url"),
                fetch_method=method,
                retrieved_at_utc=probed_at,
                matched=True,
            )
    return out


def match_file_meta(html_path: str, run_meta: dict[str, FileMeta]) -> FileMeta:
    """
    Match an on-disk HTML file (which may have been renamed, e.g. during
    manual download/upload) back to its summary.json entry using the same
    target_requests/target_playwright naming convention the probe itself
    uses, rather than requiring an exact filename match.
    """
    base = os.path.basename(html_path).lower()
    for key, meta in run_meta.items():
        key_l = key.lower()
        # exact match first
        if base == key_l:
            return meta
    # fall back to substring match on target name + method
    for key, meta in run_meta.items():
        stem = os.path.splitext(key)[0].lower()  # e.g. scores24_tournament_1_requests
        parts = stem.split("_")
        if len(parts) >= 2:
            target_tag = "_".join(parts[:-1]) if parts[-1] in ("requests", "playwright") else stem
            method_tag = parts[-1]
        else:
            target_tag, method_tag = stem, ""
        if target_tag and target_tag in base and method_tag and method_tag in base:
            return meta
    return FileMeta(matched=False)


def parse_html_file(
    html_path: str, meta: FileMeta
) -> tuple[list[dict], list[dict]]:
    """
    Returns (records, review_queue_items) for a single saved HTML file.
    """
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    source_file = os.path.basename(html_path)
    review_items: list[dict] = []

    try:
        state = extract_react_query_state(html)
    except ExtractionError as e:
        review_items.append(
            {
                "type": "EXTRACTION_FAILURE",
                "sourceFile": source_file,
                "detail": str(e),
            }
        )
        return [], review_items

    records: list[dict] = []
    for mq in iter_leagues_matches_queries(state):
        if mq.status not in ("live", "not_started", "ended"):
            review_items.append(
                {
                    "type": "UNEXPECTED_STATUS_VALUE",
                    "sourceFile": source_file,
                    "detail": f"leaguesMatches query had status={mq.status!r}",
                }
            )
            continue
        for edge in mq.edges:
            node = edge.get("node")
            if not isinstance(node, dict):
                review_items.append(
                    {
                        "type": "MALFORMED_EDGE",
                        "sourceFile": source_file,
                        "detail": "edge with no node dict",
                    }
                )
                continue
            record, notes = parse_match_node(
                node,
                status=mq.status,
                source_league_slug=mq.league_slug,
                source_url=meta.source_url,
                source_file=source_file,
                retrieved_at_utc=meta.retrieved_at_utc,
                fetch_method=meta.fetch_method,
            )
            if not meta.matched:
                notes.append("source file could not be matched to a summary.json entry -- sourceUrl/retrievedAtUTC/fetchMethod are null, not guessed")
            records.append(record)
            if notes:
                review_items.append(
                    {
                        "type": "PARSE_NOTE",
                        "matchId": record["matchId"],
                        "sourceFile": source_file,
                        "notes": notes,
                    }
                )

    return records, review_items


_STATUS_PRECEDENCE = {"not_started": 0, "live": 1, "ended": 2}


def merge_and_dedupe(all_records: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Dedupe by matchId (the Scores24-internal id).

    Two distinct situations are handled differently, deliberately:

    1. Same matchId, same status/isFinished/isLive across occurrences
       (e.g. the identical match appearing in both the 'requests' and
       'playwright' fetch of the same page -- confirmed byte-identical
       during the probe). This is a true duplicate: keep the richer
       record (prefer isFinished, then a non-null finalScore, then more
       setScores) and log it as a plain dedupe, no review needed.

    2. Same matchId, but CONFLICTING status/isLive/isFinished/resultScore
       across occurrences (confirmed to happen for real: two matches in
       the probe data appeared in both the 'live' and 'not_started'
       query buckets of the SAME single fetch, with different
       status.code/resultScore in each). This is not a simple duplicate
       -- it's Scores24's own backend returning inconsistent snapshots
       for the two query buckets. We do NOT silently pick a side. A
       deterministic default is kept (status precedence
       not_started < live < ended, i.e. the "furthest along" bucket
       wins) so the pipeline still produces one record, but the
       conflict -- with BOTH raw snapshots preserved -- is always
       written to the review queue as a STATUS_CONFLICT for a human to
       confirm or override.
    """
    by_id: dict[str, dict] = {}
    dupe_log: list[dict] = []
    status_conflicts: list[dict] = []

    def _richness(r: dict) -> tuple:
        return (
            1 if r.get("isFinished") else 0,
            1 if r.get("finalScore") is not None else 0,
            len(r.get("setScores") or []),
        )

    def _is_conflicting(a: dict, b: dict) -> bool:
        return (
            a.get("status") != b.get("status")
            or a.get("isFinished") != b.get("isFinished")
            or a.get("isLive") != b.get("isLive")
        )

    for r in all_records:
        mid = r.get("matchId")
        if mid is None:
            by_id[f"__no_id__{len(by_id)}"] = r
            continue
        if mid not in by_id:
            by_id[mid] = r
            continue

        existing = by_id[mid]

        if _is_conflicting(r, existing):
            status_conflicts.append(
                {
                    "type": "STATUS_CONFLICT",
                    "matchId": mid,
                    "detail": (
                        "Scores24 returned different status/isLive/isFinished/resultScore "
                        "for the same matchId across two query buckets or fetches -- not a "
                        "plain duplicate, needs human confirmation."
                    ),
                    "snapshotA": {
                        "sourceFile": existing.get("sourceFile"),
                        "status": existing.get("status"),
                        "isLive": existing.get("isLive"),
                        "isFinished": existing.get("isFinished"),
                        "resultScoreRaw": existing.get("liveScoreSnapshot", {}).get("scoreSoFar")
                        if existing.get("liveScoreSnapshot")
                        else existing.get("finalScore"),
                    },
                    "snapshotB": {
                        "sourceFile": r.get("sourceFile"),
                        "status": r.get("status"),
                        "isLive": r.get("isLive"),
                        "isFinished": r.get("isFinished"),
                        "resultScoreRaw": r.get("liveScoreSnapshot", {}).get("scoreSoFar")
                        if r.get("liveScoreSnapshot")
                        else r.get("finalScore"),
                    },
                    "defaultKept": None,  # filled in below
                }
            )
            a_prec = _STATUS_PRECEDENCE.get(existing.get("status"), -1)
            b_prec = _STATUS_PRECEDENCE.get(r.get("status"), -1)
            winner = r if b_prec > a_prec else existing
            status_conflicts[-1]["defaultKept"] = winner.get("status")
            by_id[mid] = winner
            continue

        if _richness(r) > _richness(existing):
            dupe_log.append(
                {
                    "matchId": mid,
                    "action": "replaced_with_richer_duplicate",
                    "keptSourceFile": r.get("sourceFile"),
                    "droppedSourceFile": existing.get("sourceFile"),
                }
            )
            by_id[mid] = r
        else:
            dupe_log.append(
                {
                    "matchId": mid,
                    "action": "kept_existing_duplicate_discarded",
                    "keptSourceFile": existing.get("sourceFile"),
                    "droppedSourceFile": r.get("sourceFile"),
                }
            )

    return list(by_id.values()), dupe_log, status_conflicts


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--summary", required=True, help="path to the probe/fetch run's summary.json")
    ap.add_argument("--html", action="append", required=True, help="path to a saved league HTML file (repeatable)")
    ap.add_argument("--out", required=True, help="output path for the parsed JSON")
    args = ap.parse_args(argv)

    run_meta = load_run_metadata(args.summary)

    all_records: list[dict] = []
    all_review: list[dict] = []
    per_file_report = []

    for html_path in args.html:
        meta = match_file_meta(html_path, run_meta)
        records, review_items = parse_html_file(html_path, meta)
        all_records.extend(records)
        all_review.extend(review_items)
        per_file_report.append(
            {
                "file": os.path.basename(html_path),
                "matched": meta.matched,
                "sourceUrl": meta.source_url,
                "fetchMethod": meta.fetch_method,
                "recordCount": len(records),
            }
        )

    merged, dupe_log, status_conflicts = merge_and_dedupe(all_records)
    merged.sort(key=lambda r: (r.get("dateISO") or "", r.get("matchId") or ""))

    all_review.extend(status_conflicts)

    output = {
        "runMeta": {
            "summarySource": os.path.basename(args.summary),
            "filesParsed": per_file_report,
            "activeLeagueSlugs": sorted(ACTIVE_LEAGUE_SLUGS),
        },
        "records": merged,
        "reviewQueue": all_review,
        "dedupeLog": dupe_log,
        "counts": {
            "totalRecords": len(merged),
            "activeLeagueRecords": sum(1 for r in merged if r.get("isActiveLeague")),
            "archivalLeagueRecords": sum(1 for r in merged if not r.get("isActiveLeague")),
            "finished": sum(1 for r in merged if r.get("isFinished")),
            "live": sum(1 for r in merged if r.get("isLive")),
            "notStarted": sum(1 for r in merged if not r.get("isFinished") and not r.get("isLive")),
            "reviewQueueItems": len(all_review),
            "statusConflicts": len(status_conflicts),
        },
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Wrote {len(merged)} records ({len(all_review)} review-queue notes) -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
