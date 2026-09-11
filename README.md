[README.md](https://github.com/user-attachments/files/32082373/README.md)
# tt-elite-scraper

Dedicated repo for the TT Elite data pipeline only — kept separate from the
TT Elite Terminal UI (which lives in its own published artifact).

Scope is deliberately narrow: **Scores24 only.** AiScore cross-checking
comes later, once Scores24 ingestion itself is real and working. The
prediction model, its weights, and the betting logic are never touched by
anything in this repo.

## Status

- **Probe phase: done.** `probe/probe.py` fetched both candidate league URLs
  two ways (plain HTTP, headless browser) and saved the raw HTML — that's
  what's in `probe/output/`.
- **Structure investigation: done.** The real match data lives in an inline
  `window.__REACT_QUERY_STATE__` hydration payload, no JS execution needed.
  See `parser/SCHEMA.md` for the full writeup.
- **Parser: built, not yet wired to a schedule or connected to the
  Terminal.** `parser/parse_scores24.py` turns saved HTML into normalized,
  deduplicated match records. It's been run against the real probe output in
  this repo and reviewed — see `parser/SCHEMA.md` and `parser/output/`.
- **Pagination: unverified.** `probe/verify_pagination.py` +
  `.github/workflows/verify_pagination.yml` test whether the `leaguesMatches`
  endpoint can be called directly with a cursor for historical backfill.
  Not yet run — run it from the Actions tab when ready.
- **Not yet built:** a scheduled production workflow (fetch → parse → commit),
  and anything that writes into the Terminal's database.

## Files to add/update in the repo

- `.github/workflows/probe.yml` (unchanged from the probe phase)
- `.github/workflows/verify_pagination.yml` (new)
- `probe/probe.py`, `probe/requirements.txt` (unchanged)
- `probe/verify_pagination.py` (new)
- `parser/parse_scores24.py` (new)
- `parser/SCHEMA.md` (new)
- `README.md` (this file)

Push these to `main`. `parser/output/` in this delivered copy already
contains a real run of the parser against the HTML you already fetched
(`probe/output/`), included so the parsed records can be reviewed without
running anything — nothing in it is synthetic.

## Running the pagination probe (optional, needed before real backfill)

Actions tab → "TT Elite pagination verification probe" → Run workflow. It
tries calling the `leaguesMatches` rapi endpoint directly (with and without
a cursor param) and saves whatever comes back to
`probe/output/pagination_probe.json` — success or failure, no assumptions
baked in. This only needs to run once before pagination gets wired into a
production fetch step.

## What happens after review

Once you've reviewed the schema, parser code, and parsed sample records
(and run the pagination probe), the next step is a production workflow that
runs the fetch + parse on a schedule and commits structured JSON — which a
scheduled Claude session then reads and proposes merging into the TT Elite
Terminal database, going through the same review-queue gate for anything
uncertain. That connection is intentionally not built yet. AiScore
cross-checking is a further addition after Scores24 ingestion is solid. The
prediction model, its weights, and the betting logic are never touched by
anything in this repo.
