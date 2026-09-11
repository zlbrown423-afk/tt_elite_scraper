#!/usr/bin/env python3
"""
Offline test harness for parser/parse_scores24.py's dual input-schema
support.

Background: production reported finished=0, notStarted=816, range=[None,
None] on a run that only used fetcher/fetch_scores24.py (direct RAPI calls,
no HTML). parse_match_node was written against the `window.__REACT_QUERY_
STATE__` hydration payload's camelCase keys (isFinished, isLive, matchDate,
resultScore, resultScores, leagueSlug); the RAPI response actually uses
snake_case (is_finished, is_live, match_date, result_score, result_scores,
league_slug) -- confirmed directly from fetcher/output/raw/tt-elite-series-
1_ended_page0.json. Every camelCase .get() on a RAPI node silently returned
None, so is_finished/is_live were always false (-> everything bucketed as
"not started") and dateISO was always None (-> range=[None,None]).

This file proves parse_match_node now normalizes BOTH shapes to the exact
same output record -- for the finished, live, and not-started cases -- and
also runs the fix against the real captured production fixture.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import parse_scores24 as ps  # noqa: E402

PASS = 0
FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS: {label}")
    else:
        FAIL += 1
        print(f"FAIL: {label}  {detail}")


COMMON_KWARGS = dict(
    status="ended",
    source_league_slug="tt-elite-series-1",
    source_url="https://scores24.live/rapi/leagues/table-tennis/tt-elite-series-1/matches",
    source_file="tt-elite-series-1_ended_page0.json",
    retrieved_at_utc="2026-09-11T00:48:08.872335+00:00",
    fetch_method="api",
)


def _teams():
    return [
        {"id": "ts_a", "slug": "lebek-marian", "name": "Marian Lebek"},
        {"id": "ts_b", "slug": "durak-kamil", "name": "Durak, Kamil"},
    ]


def camel_node(**overrides):
    node = {
        "id": "ts_dj2ry4t9yxd4r1z",
        "matchDate": "2026-09-11T00:00:00.000000Z",
        "teams": _teams(),
        "resultScore": "1:3",
        "resultScores": [
            {"type": "1", "value": "11:7"},
            {"type": "FT", "value": "1:3"},
        ],
        "slug": "11-09-2026-marian-lebek-kamil-durak",
        "leagueSlug": "tt-elite-series-1",
        "winner": 2,
        "isLive": False,
        "isFinished": True,
        "serving": None,
    }
    node.update(overrides)
    return node


def snake_node(**overrides):
    node = {
        "id": "ts_dj2ry4t9yxd4r1z",
        "match_date": "2026-09-11T00:00:00.000000Z",
        "teams": _teams(),
        "result_score": "1:3",
        "result_scores": [
            {"type": "1", "value": "11:7"},
            {"type": "FT", "value": "1:3"},
        ],
        "slug": "11-09-2026-marian-lebek-kamil-durak",
        "league_slug": "tt-elite-series-1",
        "winner": 2,
        "is_live": False,
        "is_finished": True,
        "serving": None,
    }
    node.update(overrides)
    return node


# --------------------------------------------------------------------
# Test 1: a finished match, camelCase (hydration) vs snake_case (RAPI),
# normalize to the byte-identical record and notes.
# --------------------------------------------------------------------
def test_finished_match_both_schemas_match():
    record_camel, notes_camel = ps.parse_match_node(camel_node(), **COMMON_KWARGS)
    record_snake, notes_snake = ps.parse_match_node(snake_node(), **COMMON_KWARGS)

    check("finished: camel and snake produce identical record", record_camel == record_snake,
          f"camel={record_camel}\nsnake={record_snake}")
    check("finished: camel and snake produce identical notes", notes_camel == notes_snake,
          f"camel={notes_camel}\nsnake={notes_snake}")
    check("finished: isFinished is True (not silently False)", record_snake["isFinished"] is True)
    check("finished: dateISO populated from match_date", record_snake["dateISO"] == "2026-09-11T00:00:00.000000Z")
    check("finished: finalScore populated", record_snake["finalScore"] == "1:3")
    check("finished: setScores excludes the FT placeholder", record_snake["setScores"] == [{"set": "1", "value": "11:7"}])


# --------------------------------------------------------------------
# Test 2: a live match -- exercises the liveScoreSnapshot/_field(resultScore)
# path under both schemas.
# --------------------------------------------------------------------
def test_live_match_both_schemas_match():
    live_overrides = dict(
        isLive=True, isFinished=False, winner=None, resultScore="2:1",
        resultScores=[{"type": "1", "value": "11:8"}, {"type": "FT", "value": "2:1"}],
        serving="home",
    )
    live_overrides_snake = dict(
        is_live=True, is_finished=False, winner=None, result_score="2:1",
        result_scores=[{"type": "1", "value": "11:8"}, {"type": "FT", "value": "2:1"}],
        serving="home",
    )
    record_camel, notes_camel = ps.parse_match_node(camel_node(**live_overrides), **COMMON_KWARGS)
    record_snake, notes_snake = ps.parse_match_node(snake_node(**live_overrides_snake), **COMMON_KWARGS)

    check("live: camel and snake produce identical record", record_camel == record_snake,
          f"camel={record_camel}\nsnake={record_snake}")
    check("live: camel and snake produce identical notes", notes_camel == notes_snake)
    check("live: isLive True, isFinished False", record_snake["isLive"] is True and record_snake["isFinished"] is False)
    check("live: liveScoreSnapshot populated with scoreSoFar from resultScore",
          record_snake["liveScoreSnapshot"] == {
              "scoreSoFar": "2:1",
              "setsSoFar": [{"set": "1", "value": "11:8"}],
              "serving": "home",
          }, f"{record_snake['liveScoreSnapshot']}")
    check("live: finalScore stays null (not the in-progress score)", record_snake["finalScore"] is None)


# --------------------------------------------------------------------
# Test 3: a not-started match -- this is the exact bucket production
# mis-reported as 816/816 with range=[None,None]. Confirms dateISO is
# still populated from match_date even when isFinished/isLive are both
# false, and the FT 0:0 placeholder never leaks into finalScore/setScores.
# --------------------------------------------------------------------
def test_not_started_match_both_schemas_match():
    ns_overrides = dict(
        isLive=False, isFinished=False, winner=None, resultScore=None,
        resultScores=[{"type": "FT", "value": "0:0"}],
    )
    ns_overrides_snake = dict(
        is_live=False, is_finished=False, winner=None, result_score=None,
        result_scores=[{"type": "FT", "value": "0:0"}],
    )
    record_camel, notes_camel = ps.parse_match_node(
        camel_node(**ns_overrides), status="not_started",
        **{k: v for k, v in COMMON_KWARGS.items() if k != "status"},
    )
    record_snake, notes_snake = ps.parse_match_node(
        snake_node(**ns_overrides_snake), status="not_started",
        **{k: v for k, v in COMMON_KWARGS.items() if k != "status"},
    )

    check("not_started: camel and snake produce identical record", record_camel == record_snake,
          f"camel={record_camel}\nsnake={record_snake}")
    check("not_started: camel and snake produce identical notes", notes_camel == notes_snake)
    check("not_started: dateISO still populated (this is the production bug's exact symptom)",
          record_snake["dateISO"] == "2026-09-11T00:00:00.000000Z")
    check("not_started: isFinished False, isLive False", record_snake["isFinished"] is False and record_snake["isLive"] is False)
    check("not_started: finalScore null, setScores empty (FT 0:0 placeholder never surfaced)",
          record_snake["finalScore"] is None and record_snake["setScores"] == [])
    check("not_started: liveScoreSnapshot null", record_snake["liveScoreSnapshot"] is None)


# --------------------------------------------------------------------
# Test 4: a snake_case node missing the camelCase key entirely (the real
# RAPI shape -- no isFinished/isLive/matchDate/... keys at all, not just
# falsy ones) still resolves via the snake_case fallback.
# --------------------------------------------------------------------
def test_snake_only_node_no_camel_keys_present():
    node = snake_node()
    for camel_key in ps._DUAL_SCHEMA_FIELDS:
        check(f"snake_only: {camel_key!r} is absent from a real RAPI-shaped node", camel_key not in node)

    record, _ = ps.parse_match_node(node, **COMMON_KWARGS)
    check("snake_only: isFinished resolved True via snake_case fallback", record["isFinished"] is True)
    check("snake_only: dateISO resolved via snake_case fallback", record["dateISO"] == "2026-09-11T00:00:00.000000Z")
    check("snake_only: sourceLeagueSlug resolved via snake_case fallback", record["sourceLeagueSlug"] == "tt-elite-series-1")


# --------------------------------------------------------------------
# Test 5: run the fix against the real captured production fixture --
# the exact file that triggered the finished=0/notStarted=816 report.
# --------------------------------------------------------------------
def test_real_production_fixture_parses_correctly():
    fixture_path = os.path.join(
        os.path.dirname(__file__), "..", "fetcher", "output", "raw",
        "tt-elite-series-1_ended_page0.json",
    )
    if not os.path.exists(fixture_path):
        check("real_fixture: fixture file present", False, f"missing: {fixture_path}")
        return

    with open(fixture_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    edges = raw["data"]["edges"]
    check("real_fixture: fixture has edges", len(edges) > 0)

    records = []
    for edge in edges:
        node = edge["node"]
        record, _ = ps.parse_match_node(
            node,
            status="ended",
            source_league_slug="tt-elite-series-1",
            source_url="https://scores24.live/rapi/leagues/table-tennis/tt-elite-series-1/matches",
            source_file="tt-elite-series-1_ended_page0.json",
            retrieved_at_utc="2026-09-11T00:48:08.872335+00:00",
            fetch_method="api",
        )
        records.append(record)

    finished_count = sum(1 for r in records if r["isFinished"])
    dated_count = sum(1 for r in records if r["dateISO"] is not None)

    check("real_fixture: every 'ended'-bucket record from real data comes back isFinished=True",
          finished_count == len(records), f"{finished_count}/{len(records)}")
    check("real_fixture: every record has a real dateISO (not the None/None production bug)",
          dated_count == len(records), f"{dated_count}/{len(records)}")
    check("real_fixture: first record's finalScore matches the raw result_score",
          records[0]["finalScore"] == edges[0]["node"]["result_score"],
          f"{records[0]['finalScore']!r} vs {edges[0]['node']['result_score']!r}")


if __name__ == "__main__":
    test_finished_match_both_schemas_match()
    test_live_match_both_schemas_match()
    test_not_started_match_both_schemas_match()
    test_snake_only_node_no_camel_keys_present()
    test_real_production_fixture_parses_correctly()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
