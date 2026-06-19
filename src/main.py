"""
main.py
=======
Full KOL identifier pipeline — end-to-end orchestration.

Pipeline stages
---------------
  1. DISCOVER  SeedDiscoverer  → keyword search → seed usernames
  2. SCRAPE    ThreadsScraper  → posts, profiles, comments per account
  3. FILTER    NicheFilter     → drop off-niche accounts
  4. SCORE     MonetizationScorer → rank by monetization potential
  5. OUTREACH  OutreachManager → build + review personalised DM queue

CLI usage
---------
  # Full pipeline: discover seeds from niche keywords, then scrape + score
  python src/main.py --mode discover

  # Skip discovery, scrape a known username list instead
  python src/main.py --mode scrape --usernames tarotbytara daily_astro

  # Offline mode (no network, uses fixture data)
  python src/main.py --mode discover --offline

  # Skip outreach queue (score only)
  python src/main.py --mode discover --no-outreach

  # Load previously saved seeds instead of re-searching
  python src/main.py --mode scrape --seeds-file data/raw/seeds.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import yaml
from dotenv import load_dotenv

from scraper.threads_scraper import ThreadsScraper
from scraper.parser import ThreadsParser
from scraper.profile_scraper import ProfileScraper
from scraper.comment_scraper import CommentScraper
from scraper.seed_discoverer import SeedDiscoverer
from scraper.niche_filter import NicheFilter
from scraper.monetization_scorer import MonetizationScorer
from scraper.outreach_manager import OutreachManager
from scraper.exporter import Exporter
from scraper.utils.logger import get_logger

ROOT       = Path(__file__).resolve().parents[1]
DATA_DIR   = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
CONFIG_DIR = ROOT / "config"

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------

def load_settings(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def ensure_dirs() -> None:
    for d in [
        OUTPUT_DIR,
        DATA_DIR / "raw",
        DATA_DIR / "processed",
        DATA_DIR / "seeds",
    ]:
        d.mkdir(parents=True, exist_ok=True)


def parse_args(default_usernames: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Threads KOL Identifier — full pipeline"
    )
    p.add_argument(
        "--mode",
        choices=["discover", "scrape"],
        default="discover",
        help=(
            "discover: use SeedDiscoverer to find accounts from niche keywords. "
            "scrape: use --usernames or --seeds-file directly."
        ),
    )
    p.add_argument(
        "-u", "--usernames",
        nargs="+",
        default=default_usernames,
        help="Explicit usernames to scrape (used in scrape mode).",
    )
    p.add_argument(
        "--seeds-file",
        type=Path,
        default=None,
        help="Path to a previously saved seeds JSON file (skips discovery).",
    )
    p.add_argument(
        "--offline",
        action="store_true",
        help="Use local fixture data — no live HTTP requests.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max posts to fetch per user.",
    )
    p.add_argument(
        "--no-outreach",
        action="store_true",
        help="Skip outreach queue generation (score only).",
    )
    p.add_argument(
        "--min-tier",
        choices=["A-tier", "B-tier", "C-tier"],
        default="B-tier",
        help="Minimum KOL tier to include in outreach queue.",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Stage 1 — Seed discovery
# ---------------------------------------------------------------------------

def stage_discover(
    scraper: ThreadsScraper,
    settings: Dict[str, Any],
    args: argparse.Namespace,
) -> List[str]:
    """
    Run SeedDiscoverer to find seed usernames from niche keywords.
    Saves discovered seeds to data/seeds/seeds.json for reuse.
    """
    # Load seeds from file if provided (skip network search)
    if args.seeds_file and Path(args.seeds_file).exists():
        sd = SeedDiscoverer(scraper=scraper, config_path=CONFIG_DIR / "niche_config.yaml")
        seeds = sd.load(args.seeds_file)
        logger.info(f"[main] Loaded {len(seeds)} seeds from {args.seeds_file}")
        return seeds

    niche_config = CONFIG_DIR / "niche_config.yaml"
    if not niche_config.exists():
        logger.warning(
            "[main] niche_config.yaml not found. "
            "Falling back to settings.yaml usernames."
        )
        return settings.get("usernames", [])

    sd    = SeedDiscoverer(scraper=scraper, config_path=niche_config)
    seeds = sd.discover()
    # Store reference so main() can access uid_cache
    stage_discover._last_discoverer = sd

    if not seeds:
        logger.warning("[main] Discovery returned no seeds. Check your keywords.")
        return settings.get("usernames", [])

    # Save for reuse
    seeds_path = DATA_DIR / "seeds" / "seeds.json"
    sd.export(seeds, seeds_path)
    return seeds


# ---------------------------------------------------------------------------
# Stage 2 — Scrape posts, profiles, comments
# ---------------------------------------------------------------------------

def stage_scrape(
    usernames:  List[str],
    scraper:    ThreadsScraper,
    profiler:   ProfileScraper,
    commenter:  CommentScraper,
    parser:     ThreadsParser,
    settings:   Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    For each username, fetch posts concurrently via RSSHub, profile, and comment summaries.
    Returns a list of account dicts ready for niche filtering.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    accounts: List[Dict[str, Any]] = []
    limit = settings.get("limit", 50)

    def process_user(username: str) -> Optional[Dict[str, Any]]:
        try:
            logger.info(f"[main] Scraping @{username} via RSSHub")

            # Posts
            raw_items    = scraper.fetch_user_threads(username=username, limit=limit)
            parsed_posts = [parser.parse_item(item, default_username=username)
                            for item in raw_items]
            parsed_posts = [p for p in parsed_posts if p]

            if not parsed_posts:
                logger.warning(f"[main] No posts for @{username} — skipping")
                return None

            # Profile
            profile = profiler.fetch(username)

            # Comments (attach summary to each post)
            enriched_posts = commenter.fetch_for_posts(parsed_posts, delay=0.1)

            return {
                "profile": profile,
                "posts":   enriched_posts,
            }
        except Exception as e:
            logger.exception(f"[main] Scrape failed for @{username}: {e}")
            return None

    import time
    # Concurrent RSSHub requests (Reduced to 2 workers to prevent RSSHub 503 rate limits)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {}
        for u in usernames:
            futures[pool.submit(process_user, u)] = u
            time.sleep(0.5)  # Stagger requests to give RSSHub breathing room
        for future in as_completed(futures):
            res = future.result()
            if res:
                accounts.append(res)

    logger.info(f"[main] Scraped {len(accounts)} accounts from {len(usernames)} usernames")
    return accounts


# ---------------------------------------------------------------------------
# Stage 3 — Niche filtering
# ---------------------------------------------------------------------------

def stage_filter(
    accounts: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Apply NicheFilter to drop accounts that don't match the niche.
    Skipped if niche_config.yaml has no bio/post keywords defined.
    """
    niche_config = CONFIG_DIR / "niche_config.yaml"
    nf = NicheFilter(config_path=niche_config if niche_config.exists() else None)

    # If no keywords configured, skip filtering entirely
    if not nf._bio_kws and not nf._post_kws:
        logger.info("[main] No niche keywords configured — skipping niche filter")
        return accounts

    filtered = nf.filter_many(accounts)
    logger.info(
        f"[main] Niche filter: {len(filtered)}/{len(accounts)} accounts passed"
    )
    return filtered


# ---------------------------------------------------------------------------
# Stage 4 — Scoring
# ---------------------------------------------------------------------------

def stage_score(
    accounts: List[Dict[str, Any]],
    min_tier: str,
) -> List[Dict[str, Any]]:
    """
    Score all niche-confirmed accounts and return the shortlist
    filtered to min_tier and above.
    """
    scorer  = MonetizationScorer(config_path=CONFIG_DIR / "scorer.yaml")
    results = scorer.score_many(accounts)
    leads   = scorer.filter(results, min_tier=min_tier)

    logger.info(
        f"[main] Scored {len(results)} accounts | "
        f"{len(leads)} qualify at {min_tier}+"
    )
    return results, leads


# ---------------------------------------------------------------------------
# Stage 5 — Outreach queue
# ---------------------------------------------------------------------------

def stage_outreach(
    leads:    List[Dict[str, Any]],
    accounts: List[Dict[str, Any]],
    min_tier: str,
) -> None:
    """
    Build, interactively review, and export the outreach queue.
    """
    # Build a username → profile lookup for OutreachManager
    profiles = {
        a["profile"]["username"]: a["profile"]
        for a in accounts
        if a.get("profile", {}).get("username")
    }

    om    = OutreachManager(config_path=CONFIG_DIR / "outreach.yaml")
    queue = om.build_queue(
        scored_results=leads,
        profiles=profiles,
    )

    if not queue:
        logger.info("[main] Outreach queue is empty — no accounts qualified")
        return

    # Show summary before review
    summary = om.queue_summary(queue)
    logger.info(
        f"[main] Outreach queue: {summary['total']} drafts | "
        f"A={summary['by_tier']['A-tier']} "
        f"B={summary['by_tier']['B-tier']} "
        f"C={summary['by_tier']['C-tier']}"
    )

    # Interactive CLI review — approve / skip / edit each draft
    queue = om.review_queue(queue)

    # Export full queue (all statuses)
    om.export_queue(queue, OUTPUT_DIR / "outreach_queue.json")

    # Export approved-only (ready for dm_sender.py)
    om.export_approved(queue, OUTPUT_DIR / "outreach_approved.json")

    approved = sum(1 for d in queue if d["status"] in ("approved", "edited"))
    logger.info(f"[main] Outreach: {approved} messages approved for sending")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv()
    ensure_dirs()

    settings_path = CONFIG_DIR / "settings.yaml"
    settings      = load_settings(settings_path)
    args          = parse_args(settings.get("usernames", []))

    # Apply CLI overrides to settings
    settings["use_offline"] = args.offline or settings.get("use_offline", False)
    settings["limit"]       = args.limit

    # ── initialise shared modules ─────────────────────────────────────
    scraper   = ThreadsScraper(settings=settings, config_dir=CONFIG_DIR, data_dir=DATA_DIR)
    profiler  = ProfileScraper(scraper=scraper)
    commenter = CommentScraper(scraper=scraper)
    parser    = ThreadsParser()
    exporter  = Exporter(output_dir=OUTPUT_DIR, data_dir=DATA_DIR)

    # ── stage 1: discover or use explicit username list ───────────────
    if args.mode == "discover":
        logger.info("[main] Mode: discover — finding seeds via keyword search")
        usernames = stage_discover(scraper, settings, args)
    else:
        logger.info("[main] Mode: scrape — using explicit username list")
        usernames = args.usernames or []

    if not usernames:
        logger.error("[main] No usernames to process. Exiting.")
        sys.exit(1)

    logger.info(f"[main] Processing {len(usernames)} accounts: {usernames[:5]}"
                f"{'...' if len(usernames) > 5 else ''}")

    # ── seed uid_cache from discovery results ────────────────────────
    # SeedDiscoverer already has user_id for accounts found via search.
    # Inject this into ThreadsScraper so it skips redundant lookups.
    if hasattr(stage_discover, '_last_discoverer'):
        sd = stage_discover._last_discoverer
        uid_map = {
            uname: sd.get_cached_user_id(uname)
            for uname in usernames
            if sd.get_cached_user_id(uname)
        }
        if uid_map:
            scraper.seed_uid_cache(uid_map)

    # ── stage 2: scrape ───────────────────────────────────────────────
    accounts = stage_scrape(
        usernames=usernames,
        scraper=scraper,
        profiler=profiler,
        commenter=commenter,
        parser=parser,
        settings=settings,
    )

    if not accounts:
        logger.warning("[main] No accounts scraped. Exiting.")
        sys.exit(0)



    # ── stage 3: niche filter ─────────────────────────────────────────
    accounts = stage_filter(accounts)

    if not accounts:
        logger.warning("[main] All accounts filtered as off-niche. Exiting.")
        sys.exit(0)

    # ── stage 4: score ────────────────────────────────────────────────
    all_results, leads = stage_score(accounts, min_tier=args.min_tier)

    # ── export raw scored results ─────────────────────────────────────
    scored_path = OUTPUT_DIR / "scored_results.json"
    with open(scored_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

    leads_path = OUTPUT_DIR / "leads.json"
    with open(leads_path, "w", encoding="utf-8") as f:
        json.dump(leads, f, ensure_ascii=False, indent=2)

    # Export flat posts CSV (original exporter format)
    all_posts = [p for a in accounts for p in a.get("posts", [])]
    if all_posts:
        exporter.to_json(all_posts, "threads_results.json")
        exporter.to_csv(all_posts,  "threads_results.csv")

    logger.info(
        json.dumps({
            "accounts_scraped":  len(accounts),
            "accounts_scored":   len(all_results),
            "leads_qualified":   len(leads),
            "min_tier":          args.min_tier,
            "scored_results":    str(scored_path),
            "leads":             str(leads_path),
        }, indent=2)
    )

    # ── stage 5: outreach ─────────────────────────────────────────────
    if not args.no_outreach and leads:
        stage_outreach(
            leads=leads,
            accounts=accounts,
            min_tier=args.min_tier,
        )
    elif args.no_outreach:
        logger.info("[main] --no-outreach flag set. Skipping outreach stage.")
    else:
        logger.info("[main] No leads qualified. Skipping outreach stage.")


if __name__ == "__main__":
    main()
