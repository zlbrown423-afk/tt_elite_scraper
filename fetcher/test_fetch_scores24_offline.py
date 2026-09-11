#!/usr/bin/env python3
"""
Offline test harness for fetcher/fetch_scores24.py.

This sandbox has no network access to scores24.live (confirmed blocked at
the proxy layer in earlier investigation), so this test mocks
requests.Session.get instead of hitting the live endpoint. It exercises
every code path that doesn't require the network itself:
  - cursor pagination driven only by each response's own pageInfo
  - the max-pages safety cap
  - missing hasNextPage / missing endCursor -> loud failure
  - retry-then-succeed on 500/429
  - fail loudly after exhausting retries
  - both accepted response shapes (nested under "data" and flat)
  - reuse of parser.parse_match_node / merge_and_dedupe producing the
    right schema, including a real STATUS_CONFLICT case

It does NOT prove the live endpoint's actual response shape -- that can
only be confirmed by an actual GitHub Actions run, which is why
fetch_scores24.py supports both shapes and fails loudly if neither matches.
"""
import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "parser"))
import fetch_scores24 as fs  # noqa: E402

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


def make_node(mid, status_code, is_finished, is_live, winner=None, result_score=None, name_a="Player One", name_b="Player Two"):
    return {
        "id": mid,
        "matchDate": "2026-09-11T00:00:00.000000Z",
        "teams": [
            {"id": f"{mid}_a", "slug": "player-one", "name": name_a},
            {"id": f"{mid}_b", "slug": "player-two", "name": name_b},
        ],
        "resultScore": result_score,
        "resultScores": [{"type": "FT", "value": result_score or "0:0"}],
        "slug": f"slug-{mid}",
        "leagueSlug": "tt-elite-series-1",
        "status": {"code": status_code, "name": None},
        "winner": winner,
        "isLive": is_live,
        "isFinished": is_finished,
        "serving": None,
    }


class FakeResponse:
    def __init__(self, status_code, json_body, url):
        self.status_code = status_code
        self._json = json_body
        self.url = url
        self.text = json.dumps(json_body) if json_body is not None else ""

    def json(self):
        return self._json


def edges_of(*nodes):
    return [{"cursor": f"cur_{n['id']}", "node": n} for n in nodes]


# --------------------------------------------------------------------
# Test 1: two-page pagination via nested {"data": {...}} shape, cursor
# comes only from the previous response's own pageInfo.
# --------------------------------------------------------------------
def test_two_page_pagination_nested_shape():
    page1_nodes = [make_node("m1", 100, True, False, winner=1, result_score="3:0")]
    page2_nodes = [make_node("m2", 100, True, False, winner=2, result_score="1:3")]

    responses = [
        FakeResponse(200, {"data": {"edges": edges_of(*page1_nodes),
                                     "pageInfo": {"hasNextPage": True, "endCursor": "CURSOR_AFTER_PAGE_1"}}},
                     "https://scores24.live/rapi/.../matches?first=1&status=ended"),
        FakeResponse(200, {"data": {"edges": edges_of(*page2_nodes),
                                     "pageInfo": {"hasNextPage": False, "endCursor": "CURSOR_AFTER_PAGE_2"}}},
                     "https://scores24.live/rapi/.../matches?first=1&status=ended&after=CURSOR_AFTER_PAGE_1"),
    ]
    seen_params = []

    def fake_get(url, params=None, headers=None, timeout=None):
        seen_params.append(dict(params))
        return responses.pop(0)

    session = mock.Mock()
    session.get.side_effect = fake_get

    pages, terminated_normally, err = fs.fetch_all_pages(
        session, league_slug="tt-elite-series-1", status="ended", first=1,
        date_between=["", "2026-09-11 00:00:00"], max_pages=10,
        timeout=5, max_retries=3, backoff_base=0.01, raw_dir=None,
    )

    check("two_page: terminated normally", terminated_normally is True and err is None, f"err={err}")
    check("two_page: fetched exactly 2 pages", len(pages) == 2, f"got {len(pages)}")
    check("two_page: page 2 request carried the cursor from page 1's response (not hardcoded)",
          seen_params[1].get("after") == "CURSOR_AFTER_PAGE_1",
          f"seen_params={seen_params}")
    check("two_page: page 1 request had no 'after' param", "after" not in seen_params[0], f"{seen_params[0]}")


# --------------------------------------------------------------------
# Test 2: flat {"edges":..., "pageInfo":...} shape also accepted
# --------------------------------------------------------------------
def test_flat_shape_accepted():
    nodes = [make_node("m3", 100, True, False, winner=1, result_score="3:1")]
    resp = FakeResponse(200, {"edges": edges_of(*nodes), "pageInfo": {"hasNextPage": False, "endCursor": "X"}},
                         "https://scores24.live/rapi/.../matches")
    session = mock.Mock()
    session.get.return_value = resp

    pages, terminated_normally, err = fs.fetch_all_pages(
        session, league_slug="tt-elite-series-1", status="ended", first=1,
        date_between=["", "2026-09-11"], max_pages=10,
        timeout=5, max_retries=3, backoff_base=0.01, raw_dir=None,
    )
    check("flat_shape: accepted and terminated normally", terminated_normally and err is None, f"err={err}")
    check("flat_shape: got 1 page with 1 edge", len(pages) == 1 and len(pages[0].edges) == 1)


# --------------------------------------------------------------------
# Test 3: unrecognized shape -> fails loudly, doesn't silently return []
# --------------------------------------------------------------------
def test_unrecognized_shape_fails_loudly():
    resp = FakeResponse(200, {"somethingElse": True}, "https://scores24.live/rapi/.../matches")
    session = mock.Mock()
    session.get.return_value = resp

    pages, terminated_normally, err = fs.fetch_all_pages(
        session, league_slug="tt-elite-series-1", status="ended", first=1,
        date_between=["", "2026-09-11"], max_pages=10,
        timeout=5, max_retries=3, backoff_base=0.01, raw_dir=None,
    )
    check("unrecognized_shape: not terminated normally", terminated_normally is False)
    check("unrecognized_shape: error message names the problem", err is not None and "did not match either known shape" in err, f"err={err}")
    check("unrecognized_shape: zero pages returned (no partial silent data)", len(pages) == 0)


# --------------------------------------------------------------------
# Test 4: max-pages safety cap -- flagged non-normal, not silently "done"
# --------------------------------------------------------------------
def test_max_pages_cap():
    def make_resp(i):
        n = [make_node(f"m{i}", 100, True, False, winner=1, result_score="3:0")]
        return FakeResponse(200, {"data": {"edges": edges_of(*n),
                                            "pageInfo": {"hasNextPage": True, "endCursor": f"cur{i}"}}},
                             "https://scores24.live/rapi/.../matches")

    call_count = {"n": 0}

    def fake_get(url, params=None, headers=None, timeout=None):
        call_count["n"] += 1
        return make_resp(call_count["n"])

    session = mock.Mock()
    session.get.side_effect = fake_get

    pages, terminated_normally, err = fs.fetch_all_pages(
        session, league_slug="tt-elite-series-1", status="ended", first=1,
        date_between=["", "2026-09-11"], max_pages=3,
        timeout=5, max_retries=3, backoff_base=0.01, raw_dir=None,
    )
    check("max_pages: stopped at exactly the cap", len(pages) == 3, f"got {len(pages)}")
    check("max_pages: flagged as NOT terminated normally", terminated_normally is False)
    check("max_pages: error explains it's a partial window", err is not None and "NOT exhausted" in err, f"err={err}")


# --------------------------------------------------------------------
# Test 5: hasNextPage true but endCursor missing -> fails loudly, no
# fabricated cursor
# --------------------------------------------------------------------
def test_missing_end_cursor_fails_loudly():
    n = [make_node("m5", 100, True, False, winner=1, result_score="3:0")]
    resp = FakeResponse(200, {"data": {"edges": edges_of(*n), "pageInfo": {"hasNextPage": True, "endCursor": ""}}},
                         "https://scores24.live/rapi/.../matches")
    session = mock.Mock()
    session.get.return_value = resp

    pages, terminated_normally, err = fs.fetch_all_pages(
        session, league_slug="tt-elite-series-1", status="ended", first=1,
        date_between=["", "2026-09-11"], max_pages=10,
        timeout=5, max_retries=3, backoff_base=0.01, raw_dir=None,
    )
    check("missing_cursor: not terminated normally", terminated_normally is False)
    check("missing_cursor: error names the missing cursor", err is not None and "endCursor is missing" in err, f"err={err}")
    check("missing_cursor: the one page fetched is still returned (not discarded)", len(pages) == 1)


# --------------------------------------------------------------------
# Test 6: retry-then-succeed on 500, and fail loudly after exhausting
# retries on a persistent 500
# --------------------------------------------------------------------
def test_retry_then_succeed():
    n = [make_node("m6", 100, True, False, winner=1, result_score="3:0")]
    ok_resp = FakeResponse(200, {"data": {"edges": edges_of(*n), "pageInfo": {"hasNextPage": False, "endCursor": "x"}}},
                            "https://scores24.live/rapi/.../matches")
    fail_resp = FakeResponse(503, None, "https://scores24.live/rapi/.../matches")
    fail_resp.text = "service unavailable"

    call_count = {"n": 0}

    def fake_get(url, params=None, headers=None, timeout=None):
        call_count["n"] += 1
        if call_count["n"] < 3:
            return fail_resp
        return ok_resp

    session = mock.Mock()
    session.get.side_effect = fake_get

    pages, terminated_normally, err = fs.fetch_all_pages(
        session, league_slug="tt-elite-series-1", status="ended", first=1,
        date_between=["", "2026-09-11"], max_pages=10,
        timeout=5, max_retries=5, backoff_base=0.001, raw_dir=None,
    )
    check("retry_then_succeed: eventually succeeded after 2 failures", terminated_normally is True, f"err={err}")
    check("retry_then_succeed: took exactly 3 calls", call_count["n"] == 3)


def test_fail_loudly_after_exhausting_retries():
    fail_resp = FakeResponse(500, None, "https://scores24.live/rapi/.../matches")
    fail_resp.text = "internal server error"

    session = mock.Mock()
    session.get.return_value = fail_resp

    pages, terminated_normally, err = fs.fetch_all_pages(
        session, league_slug="tt-elite-series-1", status="ended", first=1,
        date_between=["", "2026-09-11"], max_pages=10,
        timeout=5, max_retries=3, backoff_base=0.001, raw_dir=None,
    )
    check("exhaust_retries: not terminated normally", terminated_normally is False)
    check("exhaust_retries: error mentions the 500 status", err is not None and "500" in err, f"err={err}")
    check("exhaust_retries: session.get was called exactly max_retries times", session.get.call_count == 3, f"{session.get.call_count}")


# --------------------------------------------------------------------
# Test 7: end-to-end run_fetch with a real STATUS_CONFLICT scenario
# reusing parser.merge_and_dedupe, plus verification report shape.
# --------------------------------------------------------------------
def test_run_fetch_end_to_end_with_conflict():
    # Same matchId appears in both 'live' and 'not_started' buckets with
    # different status -- exactly the real conflict found during the probe.
    conflict_id = "ts_conflict1"
    live_node = make_node(conflict_id, 8, False, True, winner=None, result_score="0:0")
    not_started_node = make_node(conflict_id, 0, False, False, winner=None, result_score=None)
    ended_node = make_node("ts_ended1", 100, True, False, winner=2, result_score="1:3")

    def fake_get(url, params=None, headers=None, timeout=None):
        status = params.get("status")
        if status == "live":
            body = {"data": {"edges": edges_of(live_node), "pageInfo": {"hasNextPage": False, "endCursor": "a"}}}
        elif status == "not_started":
            body = {"data": {"edges": edges_of(not_started_node), "pageInfo": {"hasNextPage": False, "endCursor": "b"}}}
        elif status == "ended":
            body = {"data": {"edges": edges_of(ended_node), "pageInfo": {"hasNextPage": False, "endCursor": "c"}}}
        else:
            raise AssertionError(f"unexpected status {status}")
        return FakeResponse(200, body, f"https://scores24.live/rapi/.../matches?status={status}")

    with mock.patch("requests.Session") as SessionCls:
        instance = mock.Mock()
        instance.get.side_effect = fake_get
        SessionCls.return_value = instance

        report = fs.run_fetch(
            league_slug="tt-elite-series-1",
            statuses=["live", "not_started", "ended"],
            first=5,
            date_from=None,
            date_to=None,
            max_pages=10,
            timeout=5,
            max_retries=3,
            backoff_base=0.001,
            raw_dir=None,
        )

    v = report["verification"]
    check("e2e: run succeeded", report["runMeta"]["success"] is True)
    check("e2e: 3 pages fetched (one per status)", v["apiPagesFetched"] == 3, f"{v}")
    check("e2e: 3 raw nodes received", v["rawMatchNodesReceived"] == 3, f"{v}")
    check("e2e: 2 unique match ids after conflict-collapse", v["uniqueMatchIds"] == 2, f"{v}")
    check("e2e: exactly 1 status conflict detected", v["statusConflicts"] == 1, f"{v}")
    check("e2e: conflict item preserves BOTH snapshots", any(
        item.get("type") == "STATUS_CONFLICT" and item.get("matchId") == conflict_id
        and item.get("snapshotA") and item.get("snapshotB")
        for item in report["reviewQueue"]
    ), f"reviewQueue={report['reviewQueue']}")
    check("e2e: isActiveLeague true for tt-elite-series-1 records", all(r["isActiveLeague"] for r in report["records"]))
    check("e2e: winnerSide correctly mapped from winner=2", any(r["matchId"] == "ts_ended1" and r["winnerSide"] == "B" for r in report["records"]))
    check("e2e: pagination flagged normal for all statuses", all(v["paginationTerminatedNormally"].values()), f"{v['paginationTerminatedNormally']}")
    schema_fields = {"matchId","source","sourceLeagueSlug","isActiveLeague","dateISO","playerA","playerB",
                      "status","isFinished","isLive","winnerSide","finalScore","setScores","liveScoreSnapshot",
                      "matchSlug","retrievedAtUTC","sourceUrl","sourceFile","fetchMethod"}
    check("e2e: every record has exactly the agreed schema fields", all(set(r.keys()) == schema_fields for r in report["records"]),
          f"{[set(r.keys()) ^ schema_fields for r in report['records']]}")
    check("e2e: fetchMethod is 'api' (not 'requests'/'playwright')", all(r["fetchMethod"] == "api" for r in report["records"]))


if __name__ == "__main__":
    test_two_page_pagination_nested_shape()
    test_flat_shape_accepted()
    test_unrecognized_shape_fails_loudly()
    test_max_pages_cap()
    test_missing_end_cursor_fails_loudly()
    test_retry_then_succeed()
    test_fail_loudly_after_exhausting_retries()
    test_run_fetch_end_to_end_with_conflict()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
