[SCHEMA.md](https://github.com/user-attachments/files/32082307/SCHEMA.md)
# Normalized match record schema (Scores24 ingestion)

Produced by `parser/parse_scores24.py`. One object per match, deduplicated
by `matchId`. This is the *ingestion-side* record shape — it is deliberately
source-agnostic-looking so a future AiScore parser (deferred, not built yet)
can target the same shape, but every field here currently comes only from
Scores24.

```jsonc
{
  // ---- identity -------------------------------------------------------
  "matchId": "ts_dj2ry4t9yxd4r1z",   // Scores24-internal id. THE dedup key.
                                       // Never derived from names/dates.
  "source": "scores24",
  "sourceLeagueSlug": "tt-elite-series-1",
  "isActiveLeague": true,             // true only for tt-elite-series-1.
                                       // tt-elite-series-2 records are
                                       // still parsed and kept, just tagged
                                       // false -- see "Two league IDs" below.

  // ---- when / who -------------------------------------------------------
  "dateISO": "2026-09-11T00:00:00.000000Z",
  "playerA": {
    "name": "Kamil Durak",            // display name, cosmetically normalized
    "nameRaw": "Durak, Kamil",        // only present if normalization changed it
    "sourceId": "ts_n54qlgtgej8rvy9", // Scores24 player id -- THE identity key
    "slug": "durak-kamil"
  },
  "playerB": { "...": "same shape" },

  // ---- result -----------------------------------------------------------
  "status": "ended",                  // "live" | "not_started" | "ended"
                                       // (the Scores24 query bucket this
                                       // record came from)
  "isFinished": true,
  "isLive": false,
  "winnerSide": "B",                  // "A" | "B" | null. Derived ONLY from
                                       // Scores24's own 1-indexed `winner`
                                       // field (1->A, 2->B). Never inferred
                                       // from score orientation.
  "finalScore": "1:3",                // null unless isFinished is true --
                                       // see "Finished-signal handling" below
  "setScores": [
    {"set": "1", "value": "11:7"},
    {"set": "2", "value": "6:11"},
    {"set": "3", "value": "3:11"},
    {"set": "4", "value": "7:11"}
  ],                                   // [] unless isFinished is true

  // ---- in-progress snapshot (only when isLive) --------------------------
  "liveScoreSnapshot": {
    "scoreSoFar": "2:1",
    "setsSoFar": [{"set": "1", "value": "11:8"}, "..."],
    "serving": "home"                 // "home" | "away" | null, as given
  },                                   // null unless isLive is true

  // ---- provenance (required by spec, never fabricated) -------------------
  "matchSlug": "11-09-2026-marian-lebek-kamil-durak",
  "retrievedAtUTC": "2026-09-11T00:48:08.872335+00:00",  // from the fetch
                                       // run's own summary.json, never the
                                       // parse-time clock
  "sourceUrl": "https://scores24.live/en/table-tennis/l-tt-elite-series-1",
  "sourceFile": "scores24_tournament_1_requests.html",
  "fetchMethod": "requests"           // "requests" (production) |
                                       // "playwright" (probe-only, dropped
                                       // from the production fetch step)
}
```

## Finished-signal handling

`isFinished` (Scores24's own field) is the only thing that gates
`finalScore`/`setScores`. A `not_started` match still carries a placeholder
`resultScores: [{"type":"FT","value":"0:0"}]` in the raw payload -- that
placeholder is never surfaced in the normalized record. A `live` match has a
real, non-null `resultScore` too (it's the in-progress score), so
`resultScore is not None` is *not* usable as a "finished" signal by itself;
that's exactly why the parser checks `isFinished` and routes live data into
the separate `liveScoreSnapshot` field instead of `finalScore`/`setScores`.

## Two league IDs, two different backends

`tt-elite-series-1` match/player ids use a `ts_...` namespace.
`tt-elite-series-2` match/player ids use a completely different `ba_ba_...`
namespace (confirmed from real parsed output, e.g. league-2's Adrian Wiecek
is `ba_ba_221887`, not a `ts_...` id). This means the two leagues cannot be
cross-referenced by id at all -- if series-2 is ever pulled in as a
historical backfill source, matching its players against series-1's roster
will have to go through name-matching (with the same "flag, don't guess"
discipline used elsewhere), not id-matching. `isActiveLeague: false` records
are parsed and preserved by this parser but are not merged into any "active"
view by default.

## What still requires a human decision (never auto-resolved)

Every item below is written to the `reviewQueue` array in the parser's
output, never silently resolved:

- `STATUS_CONFLICT` -- the same `matchId` returned different
  `status`/`isLive`/`isFinished`/`resultScore` across two query buckets or
  fetches within the same run. **Confirmed to happen on real data**: two
  matches in the probe (`ts_1l4rj0td1ledq7v`, `ts_4jwq2ot5k91dr0v`) appeared
  in *both* the `live` and `not_started` buckets of the same single fetch,
  with `status.code` 8 vs 0 and `resultScore` `"0:0"` vs `null`. The parser
  keeps a deterministic default (whichever bucket is "furthest along":
  not_started < live < ended) so the pipeline still produces one record, but
  both raw snapshots are preserved in the review item and the choice is
  explicitly flagged, not hidden.
- `PARSE_NOTE` -- anything that didn't fit the deterministic mapping
  cleanly (a name with more than one comma, a team array that isn't exactly
  length 2, a `winner` set without `isFinished`, etc.). Each note carries
  the `matchId` and file it came from.
- `EXTRACTION_FAILURE` -- the `window.__REACT_QUERY_STATE__` marker wasn't
  found at all in a given file (page structure changed; needs re-probing,
  not a silent skip).

None of this touches player-identity matching against the *existing*
dashboard roster -- that cross-source matching step is intentionally out of
scope here and comes at the "connect to the Terminal" stage.
