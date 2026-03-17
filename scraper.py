"""
PapaCambridge past-paper scraper.

Scrapes pastpapers.papacambridge.com and downloads free IGCSE, AS Level,
and A2 Level past papers (qp / ms / er), organised as:

    output/
    ├── IGCSE/<Subject-Code>/<YYYY>/
    ├── AS_Level/<Subject-Code>/<YYYY>/
    └── A2_Level/<Subject-Code>/<YYYY>/
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, unquote

import aiohttp
from bs4 import BeautifulSoup
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://pastpapers.papacambridge.com"
IGCSE_LIST_URL = f"{BASE_URL}/papers/caie/igcse"
ALEVEL_LIST_URL = f"{BASE_URL}/papers/caie/as-and-a-level"
DIRECT_PDF_BASE = f"{BASE_URL}/directories/CAIE/CAIE-pastpapers/upload/"

ALLOWED_TYPES: set[str] = {"qp", "ms", "er"}

MAX_CONCURRENT = 5
DELAY = 0.5  # seconds between page fetches during discovery

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": BASE_URL,
}

# Regex for the standard CAIE filename pattern:
#   CODE_SEASONyy_TYPE.pdf          (no variant, e.g. examiner reports)
#   CODE_SEASONyy_TYPE_VARIANT.pdf  (most files)
_FNAME_RE = re.compile(
    r"^(\d{4,5})_(s|w|m)(\d{2})_([a-z]+)(?:_(\d+))?\.pdf$",
    re.IGNORECASE,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------

def parse_filename(filename: str) -> dict | None:
    """
    Parse a CAIE past-paper filename into structured metadata.

    Returns a dict with keys:
        code, season, year_2, year, type, variant, paper_number
    or None if the filename does not match the expected pattern.
    """
    m = _FNAME_RE.match(filename)
    if not m:
        return None

    code, season, year_2_str, ftype, variant = m.groups()
    year_2 = int(year_2_str)
    year = (1900 + year_2) if year_2 >= 90 else (2000 + year_2)
    paper_number = int(variant[0]) if variant else 0

    return {
        "code": code,
        "season": season.lower(),
        "year_2": year_2_str,
        "year": year,
        "type": ftype.lower(),
        "variant": variant or "",
        "paper_number": paper_number,
    }


# ---------------------------------------------------------------------------
# Output path logic
# ---------------------------------------------------------------------------

def get_output_path(
    root: str,
    curriculum: str,
    subject_folder: str,
    filename: str,
) -> Path | None:
    """
    Return the local Path where *filename* should be saved, or None if the
    file type is not in ALLOWED_TYPES.

    curriculum  : "IGCSE" or "A_Level"
    subject_folder : e.g. "Biology-0610"
    """
    meta = parse_filename(filename)
    if meta is None or meta["type"] not in ALLOWED_TYPES:
        return None

    year_str = str(meta["year"])

    if curriculum == "IGCSE":
        level_dir = "IGCSE"
    else:
        # AS papers: paper numbers 1-3; A2 papers: 4+
        if meta["paper_number"] in (0, 1, 2, 3):
            level_dir = "AS_Level"
        else:
            level_dir = "A2_Level"

    return Path(root) / level_dir / subject_folder / year_str / filename


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def fetch_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    retries: int = 3,
) -> str:
    """GET *url* and return the response text. Retries with exponential
    backoff on network errors or non-200 responses. Returns '' on failure."""
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    return await resp.text(errors="replace")
                log.warning("HTTP %s for %s", resp.status, url)
        except Exception as exc:
            log.warning("Request error (%s) for %s: %s", attempt + 1, url, exc)
        if attempt < retries:
            await asyncio.sleep(delay)
            delay *= 2
    return ""


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def parse_subject_links(html: str, curriculum: str) -> list[dict]:
    """
    Extract subject entries from a CAIE listing page (IGCSE or AS & A Level).

    Returns a list of dicts:
        {slug, url, name, curriculum}
    """
    soup = _soup(html)
    subjects: list[dict] = []
    seen: set[str] = set()

    prefix = "igcse-" if curriculum == "IGCSE" else "as-and-a-level-"
    pattern = re.compile(rf"/papers/caie/{re.escape(prefix)}.+", re.IGNORECASE)

    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        full = urljoin(BASE_URL, href)
        parsed = urlparse(full)
        path = parsed.path.rstrip("/")

        if not pattern.search(path):
            continue

        slug = path.split("/papers/caie/")[-1]
        if slug in seen:
            continue
        seen.add(slug)

        # Derive a human-readable subject folder name from slug + code
        # Slug example: "igcse-biology-0610"  →  folder "Biology-0610"
        code_match = re.search(r"(\d{4,5})$", slug)
        if not code_match:
            continue
        code = code_match.group(1)
        # Strip the leading level prefix and trailing code
        name_part = re.sub(rf"^{re.escape(prefix)}", "", slug)
        name_part = re.sub(rf"-{re.escape(code)}$", "", name_part)
        subject_name = name_part.replace("-", " ").title()
        subject_folder = f"{subject_name}-{code}"

        subjects.append(
            {
                "slug": slug,
                "url": full,
                "name": subject_name,
                "subject_folder": subject_folder,
                "curriculum": curriculum,
            }
        )

    return subjects


def parse_next_page(html: str) -> str | None:
    """Return the URL of the next listing page, or None if there is none."""
    soup = _soup(html)
    # Common patterns: rel="next" link or a "next" button
    next_link = soup.find("a", rel="next")
    if next_link and next_link.get("href"):
        return urljoin(BASE_URL, next_link["href"])
    # Fallback: look for a link whose text is "Next" or "›"
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        if text in ("Next", "›", "»", "next"):
            return urljoin(BASE_URL, a["href"])
    return None


def parse_session_links(html: str, subject_url: str) -> list[str]:
    """
    Extract session (year+term) sub-page URLs from a subject page.

    Example: .../igcse-biology-0610-2024-oct-nov
    """
    soup = _soup(html)
    sessions: list[str] = []
    seen: set[str] = set()

    # The subject slug is the last path component of subject_url
    subject_path = urlparse(subject_url).path.rstrip("/")
    slug = subject_path.split("/")[-1]  # e.g. "igcse-biology-0610"

    # Session links contain the slug followed by a year
    pattern = re.compile(
        rf"/papers/caie/{re.escape(slug)}-\d{{4}}",
        re.IGNORECASE,
    )

    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        full = urljoin(BASE_URL, href)
        path = urlparse(full).path.rstrip("/")
        if pattern.search(path) and full not in seen:
            seen.add(full)
            sessions.append(full)

    return sessions


def parse_file_links(html: str) -> list[str]:
    """
    Extract bare PDF filenames from a session page.

    Links use the pattern:
        download_file.php?files=https://.../upload/FILENAME.pdf
    We also accept direct .pdf href links as a fallback.
    """
    soup = _soup(html)
    filenames: list[str] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href: str = a["href"]

        # Primary: download_file.php wrapper
        if "download_file.php" in href:
            qs = parse_qs(urlparse(href).query)
            file_param = qs.get("files", [""])[0]
            if file_param:
                fname = os.path.basename(unquote(file_param))
                if fname.endswith(".pdf") and fname not in seen:
                    seen.add(fname)
                    filenames.append(fname)
            continue

        # Fallback: direct .pdf link
        if href.lower().endswith(".pdf"):
            fname = os.path.basename(unquote(urlparse(href).path))
            if fname not in seen:
                seen.add(fname)
                filenames.append(fname)

    return filenames


# ---------------------------------------------------------------------------
# Discovery phase
# ---------------------------------------------------------------------------

async def _fetch_all_subjects(
    session: aiohttp.ClientSession,
    list_url: str,
    curriculum: str,
) -> list[dict]:
    """Fetch the subject listing page(s) and return all subject dicts."""
    subjects: list[dict] = []
    url: str | None = list_url

    while url:
        log.info("Fetching subject list: %s", url)
        html = await fetch_with_retry(session, url)
        if not html:
            break
        subjects.extend(parse_subject_links(html, curriculum))
        url = parse_next_page(html)
        if url:
            await asyncio.sleep(DELAY)

    return subjects


async def collect_all_download_tasks(
    session: aiohttp.ClientSession,
    subjects: list[dict],
    root: str,
) -> list[dict]:
    """
    For every subject, walk all session pages and collect download tasks.

    Returns a flat list of dicts: {url: str, path: Path}
    """
    tasks: list[dict] = []
    seen_urls: set[str] = set()

    for i, subj in enumerate(subjects):
        log.info(
            "[%d/%d] Enumerating %s (%s)",
            i + 1,
            len(subjects),
            subj["name"],
            subj["curriculum"],
        )

        html = await fetch_with_retry(session, subj["url"])
        await asyncio.sleep(DELAY)
        if not html:
            continue

        sessions = parse_session_links(html, subj["url"])
        if not sessions:
            # Some subjects list files directly on the subject page
            sessions = [subj["url"]]

        for sess_url in sessions:
            sess_html = await fetch_with_retry(session, sess_url)
            await asyncio.sleep(DELAY)
            if not sess_html:
                continue

            for filename in parse_file_links(sess_html):
                pdf_url = DIRECT_PDF_BASE + filename
                if pdf_url in seen_urls:
                    continue
                seen_urls.add(pdf_url)

                path = get_output_path(
                    root,
                    subj["curriculum"],
                    subj["subject_folder"],
                    filename,
                )
                if path is None:
                    continue

                tasks.append({"url": pdf_url, "path": path})

    return tasks


# ---------------------------------------------------------------------------
# Download phase
# ---------------------------------------------------------------------------

async def download_file(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    task: dict,
    pbar: tqdm,
    counters: dict,
) -> None:
    """Download a single PDF, skipping if already present."""
    path: Path = task["path"]

    if path.exists():
        counters["skipped"] += 1
        pbar.update(1)
        return

    tmp_path = path.with_suffix(".tmp")
    async with semaphore:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            async with session.get(
                task["url"],
                headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    log.warning("HTTP %s downloading %s", resp.status, task["url"])
                    counters["failed"] += 1
                    pbar.update(1)
                    return
                with tmp_path.open("wb") as fh:
                    async for chunk in resp.content.iter_chunked(65536):
                        fh.write(chunk)
            tmp_path.rename(path)
            counters["downloaded"] += 1
        except Exception as exc:
            log.error("Failed to download %s: %s", task["url"], exc)
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            counters["failed"] += 1
        finally:
            pbar.update(1)


async def download_all(
    tasks: list[dict],
    max_concurrent: int = MAX_CONCURRENT,
) -> tuple[int, int, int]:
    """
    Download all tasks concurrently (up to *max_concurrent* at once).

    Returns (downloaded, skipped, failed).
    """
    counters = {"downloaded": 0, "skipped": 0, "failed": 0}
    semaphore = asyncio.Semaphore(max_concurrent)

    connector = aiohttp.TCPConnector(limit=max_concurrent)
    async with aiohttp.ClientSession(connector=connector) as dl_session:
        with tqdm(total=len(tasks), unit="file", desc="Downloading") as pbar:
            coros = [
                download_file(dl_session, semaphore, task, pbar, counters)
                for task in tasks
            ]
            await asyncio.gather(*coros, return_exceptions=True)

    return counters["downloaded"], counters["skipped"], counters["failed"]


# ---------------------------------------------------------------------------
# High-level entry points (called from main.py)
# ---------------------------------------------------------------------------

async def discover_subjects(
    scrape_igcse: bool = True,
    scrape_alevel: bool = True,
    subject_filter: str | None = None,
) -> list[dict]:
    """
    Crawl the listing pages and return the full subject list.

    If *subject_filter* is set (a slug like "igcse-biology-0610"), only that
    subject is returned.
    """
    subjects: list[dict] = []

    async with aiohttp.ClientSession() as session:
        if scrape_igcse:
            subjects += await _fetch_all_subjects(session, IGCSE_LIST_URL, "IGCSE")
        if scrape_alevel:
            subjects += await _fetch_all_subjects(session, ALEVEL_LIST_URL, "A_Level")

    if subject_filter:
        subjects = [s for s in subjects if s["slug"] == subject_filter]

    return subjects


async def build_task_list(subjects: list[dict], output_dir: str) -> list[dict]:
    """Walk every subject and return a flat list of download tasks."""
    async with aiohttp.ClientSession() as session:
        return await collect_all_download_tasks(session, subjects, output_dir)
