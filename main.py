"""
CLI entry point for the PapaCambridge past-paper scraper.

Usage examples:

    # Dry-run: show what would be downloaded without writing any files
    python main.py --dry-run

    # Download a single subject to ./test_output
    python main.py --subject igcse-biology-0610 --output test_output

    # Download everything (IGCSE + AS & A Level)
    python main.py --output output

    # Download only A Level papers, 10 parallel workers
    python main.py --no-igcse --workers 10 --output output

    # Adjust politeness delay between page fetches (default: 0.5 s)
    python main.py --delay 1.0 --output output
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import scraper


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download free CAIE past papers from pastpapers.papacambridge.com",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--output",
        default="output",
        metavar="DIR",
        help="Root directory for downloaded papers (default: ./output)",
    )
    parser.add_argument(
        "--no-igcse",
        dest="igcse",
        action="store_false",
        default=True,
        help="Skip IGCSE past papers",
    )
    parser.add_argument(
        "--no-alevel",
        dest="alevel",
        action="store_false",
        default=True,
        help="Skip AS & A Level past papers",
    )
    parser.add_argument(
        "--subject",
        metavar="SLUG",
        default=None,
        help=(
            "Only scrape one subject (use the slug from the URL, "
            "e.g. 'igcse-biology-0610' or 'as-and-a-level-chemistry-9701')"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover files and print the task list without downloading",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=scraper.MAX_CONCURRENT,
        metavar="N",
        help=f"Max concurrent downloads (default: {scraper.MAX_CONCURRENT})",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=scraper.DELAY,
        metavar="SECS",
        help=f"Seconds between page fetches during discovery (default: {scraper.DELAY})",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> None:
    # Apply runtime overrides to scraper constants
    scraper.MAX_CONCURRENT = args.workers
    scraper.DELAY = args.delay

    # ------------------------------------------------------------------ #
    # Phase 1: discover subjects                                           #
    # ------------------------------------------------------------------ #
    print("Discovering subjects …")
    subjects = await scraper.discover_subjects(
        scrape_igcse=args.igcse,
        scrape_alevel=args.alevel,
        subject_filter=args.subject,
    )

    if not subjects:
        print("No subjects found. Check your --subject filter or network connection.")
        sys.exit(1)

    print(f"Found {len(subjects)} subject(s).")

    # ------------------------------------------------------------------ #
    # Phase 2: build download task list                                    #
    # ------------------------------------------------------------------ #
    print("Enumerating files …")
    tasks = await scraper.build_task_list(subjects, args.output)

    if not tasks:
        print("No downloadable files found (check filters or network).")
        sys.exit(0)

    print(f"\nFound {len(tasks)} file(s) across {len(subjects)} subject(s).")

    # ------------------------------------------------------------------ #
    # Dry-run: just print the task list                                    #
    # ------------------------------------------------------------------ #
    if args.dry_run:
        print("\n--- DRY RUN (no files will be downloaded) ---")
        for task in tasks:
            status = "EXISTS" if task["path"].exists() else "PENDING"
            print(f"  [{status}]  {task['path']}")
        pending = sum(1 for t in tasks if not t["path"].exists())
        print(f"\n  {pending} pending / {len(tasks) - pending} already on disk")
        return

    # ------------------------------------------------------------------ #
    # Phase 3: download                                                    #
    # ------------------------------------------------------------------ #
    downloaded, skipped, failed = await scraper.download_all(tasks, args.workers)

    print(
        f"\nDone. "
        f"Downloaded: {downloaded}  |  "
        f"Skipped (already present): {skipped}  |  "
        f"Failed: {failed}"
    )
    if failed:
        print(
            "Some downloads failed. Re-run the same command to retry; "
            "already-downloaded files will be skipped automatically."
        )


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
