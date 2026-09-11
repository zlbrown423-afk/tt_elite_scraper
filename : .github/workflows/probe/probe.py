#!/usr/bin/env python3
"""
TT Elite data pipeline (tt-elite-scraper) -- PROBE PHASE ONLY.

Scope, deliberately narrow for this first pass: Scores24's TT Elite Series
page(s) only. AiScore cross-checking comes later, once Scores24 ingestion
itself is real and working.

This script does NOT parse or extract match data. Its only job is to fetch
the Scores24 TT Elite Series page two different ways (plain HTTP request,
and a real rendered headless browser) and save exactly what comes back,
plus a small summary. The point: from the Claude sandbox that will
eventually run the *real* ingestion, there is no way to see the site's
actual HTML/DOM -- only an AI-paraphrased view via a fetch tool. This probe
runs from GitHub Actions instead (real, unrestricted internet egress) so a
human (or Claude, reading the saved files afterward) can look at the real
markup and build a real, deterministic parser against it -- instead of
guessing.

Nothing here writes to any prediction/model code, and nothing here is
scheduled to repeat automatically yet -- this workflow is manual-trigger
only (workflow_dispatch) until the real parser exists.

Outputs (all under probe/output/, committed back to the repo by the
workflow):
  scores24_tournament_1_requests.html    -- plain requests.get(), league id 1
  scores24_tournament_1_playwright.html  -- headless Chromium, full render
  scores24_tournament_2_requests.html    -- league id 2 (may be a duplicate
  scores24_tournament_2_playwright.html     of id 1, or a separate division
                                             -- unresolved, both probed to
                                             settle it from real output)
  summary.json                        -- status codes, byte sizes, and a
                                          handful of known-name/keyword
                                          sniff tests per file, so viability
                                          can be assessed before anyone
                                          reads the raw HTML at all.
"""
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import requests

OUT_DIR = os.path.join(os.path.dirname(__file__), "output")
os.makedirs(OUT_DIR, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Real URLs confirmed to carry TT Elite Series content during manual
# investigation (2026-09-10). Scores24 only for this probe -- AiScore comes
# later. The two scores24 league ids may be the same league mirrored twice
# or two separate divisions -- unresolved, both probed so it can be settled
# by looking at the actual output.
TARGETS = {
    "scores24_tournament_1": "https://scores24.live/en/table-tennis/l-tt-elite-series-1",
    "scores24_tournament_2": "https://scores24.live/en/table-tennis/l-tt-elite-series-2",
}

# A handful of strings we *know* should appear if the real match data made
# it into the response (from real matches observed manually on 2026-09-10).
# Their presence/absence is a fast, deterministic viability signal.
SNIFF_STRINGS = [
    "Slawinski", "Baron", "Idaczyk", "Kurek", "TT Elite",
    "Elite Series", "table-tennis",
]


def sniff(text):
    lower = text.lower()
    return {s: (s.lower() in lower) for s in SNIFF_STRINGS}


def fetch_requests(url):
    t0 = time.time()
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=25,
        )
        return {
            "ok": True,
            "status": resp.status_code,
            "bytes": len(resp.content),
            "elapsed_s": round(time.time() - t0, 2),
            "final_url": resp.url,
            "content_type": resp.headers.get("content-type"),
            "text": resp.text,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "elapsed_s": round(time.time() - t0, 2),
        }


def fetch_playwright(url, browser):
    t0 = time.time()
    try:
        page = browser.new_page(user_agent=UA)
        page.set_default_timeout(20000)
        resp = page.goto(url, wait_until="networkidle")
        # give any late XHR-driven widgets a beat to paint
        page.wait_for_timeout(2500)
        html = page.content()
        status = resp.status if resp else None
        page.close()
        return {
            "ok": True,
            "status": status,
            "bytes": len(html.encode("utf-8")),
            "elapsed_s": round(time.time() - t0, 2),
            "final_url": page.url if hasattr(page, "url") else url,
            "text": html,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "elapsed_s": round(time.time() - t0, 2),
        }


def main():
    summary = {
        "probedAtUTC": datetime.now(timezone.utc).isoformat(),
        "targets": {},
    }

    # --- plain requests pass ---
    for name, url in TARGETS.items():
        print(f"[requests]   fetching {name}: {url}", file=sys.stderr)
        result = fetch_requests(url)
        entry = summary["targets"].setdefault(name, {"url": url})
        if result.get("ok"):
            fname = f"{name}_requests.html"
            with open(os.path.join(OUT_DIR, fname), "w", encoding="utf-8") as f:
                f.write(result["text"])
            entry["requests"] = {
                "status": result["status"],
                "bytes": result["bytes"],
                "elapsed_s": result["elapsed_s"],
                "content_type": result.get("content_type"),
                "final_url": result.get("final_url"),
                "sniff": sniff(result["text"]),
                "savedAs": f"probe/output/{fname}",
            }
        else:
            entry["requests"] = {"ok": False, "error": result["error"], "elapsed_s": result["elapsed_s"]}

    # --- headless browser pass (only if playwright is installed) ---
    try:
        from playwright.sync_api import sync_playwright
        have_playwright = True
    except ImportError:
        have_playwright = False
        print("[playwright] not installed -- skipping rendered-browser pass", file=sys.stderr)

    if have_playwright:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for name, url in TARGETS.items():
                print(f"[playwright] fetching {name}: {url}", file=sys.stderr)
                result = fetch_playwright(url, browser)
                entry = summary["targets"].setdefault(name, {"url": url})
                if result.get("ok"):
                    fname = f"{name}_playwright.html"
                    with open(os.path.join(OUT_DIR, fname), "w", encoding="utf-8") as f:
                        f.write(result["text"])
                    entry["playwright"] = {
                        "status": result["status"],
                        "bytes": result["bytes"],
                        "elapsed_s": result["elapsed_s"],
                        "final_url": result.get("final_url"),
                        "sniff": sniff(result["text"]),
                        "savedAs": f"probe/output/{fname}",
                    }
                else:
                    entry["playwright"] = {"ok": False, "error": result["error"], "elapsed_s": result["elapsed_s"]}
            browser.close()

    with open(os.path.join(OUT_DIR, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
