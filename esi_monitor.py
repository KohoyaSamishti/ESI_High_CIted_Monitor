#!/usr/bin/env python3
"""Monitor papers against ESI highly-cited thresholds.

Official mode reads thresholds and citation counts through a locally
authenticated Clarivate/Web of Science browser session. OpenAlex and manual
inputs are supported as explicitly labeled fallback or preview sources.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_PAPERS = "papers.csv"
DEFAULT_THRESHOLDS = "esi_thresholds.csv"
DEFAULT_STATE = ".esi_state.json"
DEFAULT_REPORT = "reports/esi_status.csv"
DEFAULT_HISTORY = "reports/esi_history.csv"
DEFAULT_WOS_SESSION_DIR = ".wos_session"
DEFAULT_INTERVAL_HOURS = 24.0
DEFAULT_THRESHOLD_PERCENTILE = 0.01
USER_AGENT = "esi-highly-cited-monitor/0.1"
ESI_THRESHOLDS_URL = "https://esi.clarivate.com/ThresholdsAction.action"
ESI_INDICATORS_URL = "https://esi.clarivate.com/IndicatorsAction.action"
ESI_INDICATORS_DATA_URL = "https://esi.clarivate.com/IndicatorsDataAction.action"
WOS_BASIC_SEARCH_URL = "https://www.webofscience.com/wos/woscc/basic-search"
WOS_PROFILE_PUBLICATIONS_URL = "https://www.webofscience.com/wos/op/publications/summary"
THRESHOLD_FIELDNAMES = [
    "esi_field",
    "pub_year",
    "highly_cited_threshold",
    "hot_paper_threshold",
    "snapshot_date",
    "source",
    "threshold_kind",
    "total_works",
    "rank",
    "openalex_filter",
    "notes",
]
OPENALEX_FIELD_BY_ESI_FIELD = {
    "agricultural sciences": "https://openalex.org/fields/15",
    "biology and biochemistry": "https://openalex.org/fields/13",
    "biology & biochemistry": "https://openalex.org/fields/13",
    "chemistry": "https://openalex.org/fields/18",
    "clinical medicine": "https://openalex.org/fields/14",
    "computer science": "https://openalex.org/fields/17",
    "economics and business": "https://openalex.org/fields/10",
    "economics business": "https://openalex.org/fields/10",
    "economics/business": "https://openalex.org/fields/10",
    "engineering": "https://openalex.org/fields/12",
    "environment and ecology": "https://openalex.org/fields/21",
    "environment ecology": "https://openalex.org/fields/21",
    "environment/ecology": "https://openalex.org/fields/21",
    "geosciences": "https://openalex.org/fields/27",
    "immunology": "https://openalex.org/fields/23",
    "materials science": "https://openalex.org/fields/19",
    "mathematics": "https://openalex.org/fields/20",
    "microbiology": "https://openalex.org/fields/23",
    "molecular biology and genetics": "https://openalex.org/fields/13",
    "molecular biology & genetics": "https://openalex.org/fields/13",
    "neuroscience and behavior": "https://openalex.org/fields/24",
    "neuroscience & behavior": "https://openalex.org/fields/24",
    "pharmacology and toxicology": "https://openalex.org/fields/26",
    "pharmacology & toxicology": "https://openalex.org/fields/26",
    "physics": "https://openalex.org/fields/16",
    "plant and animal science": "https://openalex.org/fields/15",
    "plant & animal science": "https://openalex.org/fields/15",
    "psychiatry and psychology": "https://openalex.org/fields/25",
    "psychiatry psychology": "https://openalex.org/fields/25",
    "psychiatry/psychology": "https://openalex.org/fields/25",
    "social sciences general": "https://openalex.org/fields/22",
    "social sciences, general": "https://openalex.org/fields/22",
    "space sciences": "https://openalex.org/fields/16",
}


@dataclass
class Paper:
    paper_id: str
    title: str
    doi: str
    esi_field: str
    pub_year: int | None
    manual_citations: int | None
    target_type: str
    openalex_filter: str
    notes: str


@dataclass
class CitationResult:
    count: int | None
    source: str
    title: str | None = None
    doi: str | None = None
    pub_year: int | None = None
    record_id: str | None = None
    official_listed: bool | None = None
    openalex_id: str | None = None
    openalex_field: str | None = None
    openalex_filter: str | None = None
    error: str | None = None


@dataclass
class CheckResult:
    checked_at: str
    paper_id: str
    title: str
    doi: str
    esi_field: str
    pub_year: int | None
    target_type: str
    citations: int | None
    threshold: int | None
    threshold_source: str | None
    threshold_snapshot: str | None
    margin: int | None
    progress: float | None
    status: str
    citation_source: str
    official_listed: bool | None
    evidence_title: str | None
    evidence_doi: str | None
    evidence_record_id: str | None
    evidence_openalex_id: str | None
    error: str | None
    notes: str


class MonitorError(RuntimeError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_header(value: str) -> str:
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def normalize_field_name(value: str) -> str:
    text = value.strip().casefold().replace("&", "and")
    text = re.sub(r"[\s/_-]+", " ", text)
    text = re.sub(r"\s*,\s*", ", ", text)
    return text.strip()


def esi_dataset_snapshot_date(text: str) -> str:
    match = re.search(
        r"Essential Science Indicators dataset updated\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return utc_now_iso()[:10]
    try:
        return datetime.strptime(match.group(1), "%b %d, %Y").date().isoformat()
    except ValueError:
        try:
            return datetime.strptime(match.group(1), "%B %d, %Y").date().isoformat()
        except ValueError:
            return utc_now_iso()[:10]


def normalize_doi(value: str) -> str:
    doi = (value or "").strip()
    if not doi:
        return ""
    doi = doi.removeprefix("https://doi.org/")
    doi = doi.removeprefix("http://doi.org/")
    doi = doi.removeprefix("doi:")
    return doi.strip()


def parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text.replace(",", "")))
    except ValueError as exc:
        raise MonitorError(f"Expected an integer value, got {value!r}") from exc


def try_parse_int(value: str | None) -> int | None:
    try:
        return parse_int(value)
    except MonitorError:
        return None


def read_csv_dicts(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise MonitorError(f"Missing file: {path}")
    raw = path.read_bytes()
    if raw.startswith(b"PK\x03\x04"):
        raise MonitorError(describe_zip_spreadsheet(path, raw))

    decode_errors: list[str] = []
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            text = raw.decode(encoding)
            return read_csv_rows(io.StringIO(text), str(path))
        except UnicodeDecodeError as exc:
            decode_errors.append(f"{encoding}: {exc}")
    raise MonitorError(
        f"Cannot decode {path} as a text CSV. Tried UTF-8, GB18030, and Big5. "
        f"Last errors: {'; '.join(decode_errors[-2:])}"
    )


def describe_zip_spreadsheet(path: Path, raw: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile:
        names = set()

    if any(name.startswith("Index/") and name.endswith(".iwa") for name in names):
        kind = "an Apple Numbers document"
    elif "[Content_Types].xml" in names:
        kind = "an Excel .xlsx workbook"
    else:
        kind = "a ZIP archive"

    return (
        f"{path} is {kind}, not a text CSV. Export it as CSV first "
        "(Numbers: File > Export To > CSV; Excel: Save As CSV UTF-8), "
        f"then save the exported text file as {path.name}."
    )


def read_csv_text_dicts(text: str, label: str) -> list[dict[str, str]]:
    return read_csv_rows(io.StringIO(text), label)


def read_csv_rows(lines: Any, label: str) -> list[dict[str, str]]:
    rows_as_lists = list(csv.reader(lines))
    header_index = find_header_index(rows_as_lists)
    if header_index > 0:
        rows_as_lists = rows_as_lists[header_index:]

    if not rows_as_lists:
        raise MonitorError(f"{label} has no header row")
    fieldnames = rows_as_lists[0]
    if not fieldnames:
        raise MonitorError(f"{label} has no header row")
    field_map = {index: normalize_header(name) for index, name in enumerate(fieldnames)}
    rows: list[dict[str, str]] = []
    for raw in rows_as_lists[1:]:
        row: dict[str, str] = {}
        for index, value in enumerate(raw):
            if index in field_map:
                row[field_map[index]] = (value or "").strip()
        rows.append(row)
    return rows


def find_header_index(rows: list[list[str]]) -> int:
    known_headers = {
        "paper_id",
        "title",
        "doi",
        "esi_field",
        "pub_year",
        "manual_citations",
        "target_type",
        "openalex_filter",
        "highly_cited_threshold",
        "hot_paper_threshold",
        "snapshot_date",
        "source",
        "threshold_kind",
    }
    for index, row in enumerate(rows[:10]):
        normalized = {normalize_header(cell) for cell in row}
        if len(normalized & known_headers) >= 2:
            return index
    return 0


def read_papers(path: Path) -> list[Paper]:
    rows = read_csv_dicts(path)
    papers: list[Paper] = []
    for idx, row in enumerate(rows, start=2):
        title = row.get("title", "")
        doi = normalize_doi(row.get("doi", ""))
        if not title and not doi:
            continue
        paper_id = row.get("paper_id") or doi or f"row-{idx}"
        target_type = (row.get("target_type") or "highly_cited").strip().lower()
        if target_type not in {"highly_cited", "hot_paper"}:
            raise MonitorError(f"{path}:{idx} target_type must be highly_cited or hot_paper")
        paper = Paper(
            paper_id=paper_id,
            title=title,
            doi=doi,
            esi_field=row.get("esi_field", ""),
            pub_year=parse_int(row.get("pub_year")),
            manual_citations=parse_int(row.get("manual_citations")),
            target_type=target_type,
            openalex_filter=row.get("openalex_filter", ""),
            notes=row.get("notes", ""),
        )
        if is_placeholder_paper(paper):
            continue
        papers.append(paper)
    if not papers:
        raise MonitorError(f"No real papers found in {path}; replace the sample my-paper-1 row with your paper.")
    return papers


def is_placeholder_paper(paper: Paper) -> bool:
    return (
        paper.paper_id == "my-paper-1"
        or paper.doi == "10.0000/example.doi"
        or paper.title.casefold().startswith("replace with your paper title")
    )


def read_thresholds(path: Path) -> dict[tuple[str, int], dict[str, Any]]:
    rows = read_csv_dicts(path)
    thresholds: dict[tuple[str, int], dict[str, Any]] = {}
    for idx, row in enumerate(rows, start=2):
        field = row.get("esi_field", "").strip()
        pub_year = parse_int(row.get("pub_year"))
        if not field or pub_year is None:
            continue
        key = (field.casefold(), pub_year)
        thresholds[key] = {
            "highly_cited": parse_int(row.get("highly_cited_threshold")),
            "hot_paper": parse_int(row.get("hot_paper_threshold")),
            "source": row.get("source") or "manual",
            "snapshot_date": row.get("snapshot_date", ""),
        }
    if not thresholds:
        raise MonitorError(f"No thresholds found in {path}")
    return thresholds


def openalex_url_for_paper(paper: Paper, mailto: str | None, select: str | None = None) -> str:
    params = {"select": select or "id,display_name,doi,publication_year,cited_by_count,primary_topic"}
    if mailto:
        params["mailto"] = mailto
    query = urllib.parse.urlencode(params)
    if paper.doi:
        doi_url = f"https://doi.org/{paper.doi}"
        encoded_doi = urllib.parse.quote(doi_url, safe="")
        return f"https://api.openalex.org/works/{encoded_doi}?{query}"

    search_params = {
        "search": paper.title,
        "per-page": "1",
        "select": params["select"],
    }
    if mailto:
        search_params["mailto"] = mailto
    return "https://api.openalex.org/works?" + urllib.parse.urlencode(search_params)


def fetch_json(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=request_headers(url))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_text(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers=request_headers(url))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset)


def request_headers(url: str) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    api_key = os.getenv("OPENALEX_API_KEY", "")
    if api_key and urllib.parse.urlparse(url).netloc == "api.openalex.org":
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def require_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise MonitorError(
            "Playwright is required for WoS/ESI browser automation. Install it with: "
            "python3 -m pip install playwright && python3 -m playwright install chromium"
        ) from exc
    return sync_playwright


def wos_session_dir(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "wos_session_dir", DEFAULT_WOS_SESSION_DIR))


def save_debug_page(page: Any, session_dir: Path, name: str) -> None:
    debug_dir = session_dir / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    try:
        (debug_dir / f"{name}.html").write_text(page.content(), encoding="utf-8")
        body_text = page.locator("body").inner_text(timeout=5000)
        (debug_dir / f"{name}.txt").write_text(f"URL: {page.url}\n\n{body_text}", encoding="utf-8")
    except Exception:
        pass


def open_wos_context(args: argparse.Namespace) -> tuple[Any, Any, Any]:
    sync_playwright = require_playwright()
    playwright = sync_playwright().start()
    session_dir = wos_session_dir(args)
    session_dir.mkdir(parents=True, exist_ok=True)
    headless = not getattr(args, "wos_headed", False)
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(session_dir),
        headless=headless,
        accept_downloads=True,
        viewport={"width": 1440, "height": 1000},
    )
    return playwright, context, context.pages[0] if context.pages else context.new_page()


def close_wos_context(playwright: Any, context: Any) -> None:
    context.close()
    playwright.stop()


def login_wos(args: argparse.Namespace) -> None:
    args.wos_headed = True
    playwright, context, page = open_wos_context(args)
    try:
        page.goto(args.login_url or ESI_THRESHOLDS_URL, timeout=int(args.timeout * 1000), wait_until="domcontentloaded")
        print("A browser window is open. Log in to Clarivate/WoS there, then return here and press Enter.")
        input("Press Enter after the ESI thresholds page or Web of Science is logged in...")
        page.wait_for_load_state("domcontentloaded", timeout=int(args.timeout * 1000))
        save_debug_page(page, wos_session_dir(args), "login_after")
        print(f"Saved browser session under {wos_session_dir(args)}")
    finally:
        close_wos_context(playwright, context)


def page_looks_logged_out(page_text: str) -> bool:
    lowered = page_text.casefold()
    if "currently signed in" in lowered and "stay signed in" in lowered:
        return False
    login_markers = (
        "sign in",
        "login",
        "institutional sign in",
        "email address",
        "password",
        "shibboleth",
        "single sign-on",
    )
    product_markers = ("web of science", "essential science indicators", "citation thresholds")
    return any(marker in lowered for marker in login_markers) and not any(
        marker in lowered for marker in product_markers if marker == "citation thresholds"
    )


def require_logged_in(page: Any, session_dir: Path, debug_name: str) -> None:
    text = page.locator("body").inner_text(timeout=10000)
    if page_looks_human_verification(text):
        save_debug_page(page, session_dir, debug_name)
        raise MonitorError(
            "WoS is asking for human verification. Run "
            "./esi_monitor.py login-wos --login-url https://www.webofscience.com/wos/op/publications/summary "
            "with the headed browser, complete the verification, then run the monitor again."
        )
    if page_looks_logged_out(text):
        save_debug_page(page, session_dir, debug_name)
        raise MonitorError(
            f"WoS/ESI session appears to be logged out. Run ./esi_monitor.py login-wos first. "
            f"Debug page saved under {session_dir / 'debug'}."
        )


def page_looks_human_verification(page_text: str) -> bool:
    lowered = page_text.casefold()
    return "verify you are human" in lowered or "unusual activity" in lowered


def openalex_filter_from_work(data: dict[str, Any]) -> tuple[str | None, str | None]:
    topic = data.get("primary_topic") or {}
    field = topic.get("field") or {}
    field_id = field.get("id")
    field_name = field.get("display_name")
    if field_id:
        return f"primary_topic.field.id:{field_id}", field_name
    return None, field_name


def openalex_filter_from_esi_field(esi_field: str) -> str | None:
    field_id = OPENALEX_FIELD_BY_ESI_FIELD.get(normalize_field_name(esi_field))
    if not field_id:
        return None
    return f"primary_topic.field.id:{field_id}"


def citation_result_from_openalex_data(data: dict[str, Any]) -> CitationResult:
    openalex_filter, openalex_field = openalex_filter_from_work(data)
    return CitationResult(
        count=parse_int(data.get("cited_by_count")),
        source="openalex",
        title=data.get("display_name"),
        doi=normalize_doi(data.get("doi") or ""),
        pub_year=parse_int(data.get("publication_year")),
        openalex_id=data.get("id"),
        openalex_field=openalex_field,
        openalex_filter=openalex_filter,
    )


def fetch_openalex_citations(paper: Paper, timeout: float, mailto: str | None) -> CitationResult:
    url = openalex_url_for_paper(paper, mailto)
    try:
        data = fetch_json(url, timeout=timeout)
        if "results" in data:
            results = data.get("results") or []
            if not results:
                return CitationResult(count=None, source="openalex", error="OpenAlex returned no search result")
            data = results[0]
        return citation_result_from_openalex_data(data)
    except urllib.error.HTTPError as exc:
        return CitationResult(count=None, source="openalex", error=f"OpenAlex HTTP {exc.code}: {exc.reason}")
    except urllib.error.URLError as exc:
        return CitationResult(count=None, source="openalex", error=f"OpenAlex network error: {exc.reason}")
    except TimeoutError:
        return CitationResult(count=None, source="openalex", error="OpenAlex request timed out")
    except Exception as exc:  # noqa: BLE001 - surfaced in report instead of crashing all checks.
        return CitationResult(count=None, source="openalex", error=f"OpenAlex error: {exc}")


def get_citation_result(
    paper: Paper,
    source_mode: str,
    timeout: float,
    mailto: str | None,
    args: argparse.Namespace | None = None,
) -> CitationResult:
    if source_mode == "manual":
        if paper.manual_citations is None:
            return CitationResult(count=None, source="manual", error="manual_citations is blank")
        return CitationResult(count=paper.manual_citations, source="manual")

    if source_mode == "wos-session":
        if args is None:
            return CitationResult(count=None, source="wos-session", error="internal error: missing args")
        return fetch_wos_citations(paper, args)

    if source_mode == "esi-session":
        return CitationResult(
            count=None,
            source="esi-session",
            error="internal error: esi-session citations are loaded in batches",
        )

    if source_mode == "wos-profile":
        return CitationResult(
            count=None,
            source="wos-profile",
            error="internal error: wos-profile citations are loaded in batches",
        )

    openalex_result = fetch_openalex_citations(paper, timeout=timeout, mailto=mailto)
    if source_mode == "openalex":
        return openalex_result

    if openalex_result.count is not None:
        return openalex_result
    if paper.manual_citations is not None:
        return CitationResult(
            count=paper.manual_citations,
            source="manual_fallback",
            error=openalex_result.error,
        )
    return openalex_result


def fetch_wos_citations(paper: Paper, args: argparse.Namespace) -> CitationResult:
    playwright, context, page = open_wos_context(args)
    session_dir = wos_session_dir(args)
    try:
        return fetch_wos_citations_with_page(paper, args, page, session_dir)
    except Exception as exc:
        try:
            save_debug_page(page, session_dir, f"wos_error_{safe_name(paper.paper_id)}")
        except Exception:
            pass
        return CitationResult(count=None, source="wos-session", error=f"WoS browser error: {exc}")
    finally:
        close_wos_context(playwright, context)


def fetch_wos_citations_with_page(
    paper: Paper,
    args: argparse.Namespace,
    page: Any,
    session_dir: Path,
) -> CitationResult:
    page.goto(WOS_BASIC_SEARCH_URL, timeout=int(args.timeout * 1000), wait_until="domcontentloaded")
    wait_for_page_idle(page, int(args.timeout * 1000))
    pass_wos_stay_signed_in(page, args.timeout)
    require_logged_in(page, session_dir, f"wos_logged_out_{safe_name(paper.paper_id)}")
    query = paper.doi or paper.title
    if not query:
        return CitationResult(count=None, source="wos-session", error="paper has no DOI or title")
    perform_wos_search(page, query, timeout=args.timeout)
    text = page.locator("body").inner_text(timeout=int(args.timeout * 1000))
    count = extract_wos_citation_count(text)
    if count is None:
        save_debug_page(page, session_dir, f"wos_citation_parse_failed_{safe_name(paper.paper_id)}")
        return CitationResult(
            count=None,
            source="wos-session",
            error=f"Could not parse WoS Times Cited after searching {query!r}",
        )
    return CitationResult(count=count, source="wos-session", title=paper.title, doi=paper.doi)


def fetch_esi_session_citations_for_papers(
    papers: list[Paper],
    args: argparse.Namespace,
    page: Any,
    session_dir: Path,
) -> dict[str, CitationResult]:
    page.goto(ESI_INDICATORS_URL, timeout=int(args.timeout * 1000), wait_until="domcontentloaded")
    wait_for_page_idle(page, int(args.timeout * 1000))
    require_logged_in(page, session_dir, "esi_indicators_logged_out")

    results: dict[str, CitationResult] = {}
    grouped: dict[tuple[str, int, str], list[Paper]] = {}
    for paper in papers:
        if not paper.esi_field:
            results[paper.paper_id] = CitationResult(
                count=None,
                source="esi-session",
                error="esi_field is required for official ESI lookup",
            )
            continue
        if paper.pub_year is None:
            results[paper.paper_id] = CitationResult(
                count=None,
                source="esi-session",
                error="pub_year is required for official ESI lookup",
            )
            continue
        grouped.setdefault((esi_field_query_value(paper.esi_field), paper.pub_year, paper.target_type), []).append(paper)

    for (field, year, target_type), group_papers in grouped.items():
        try:
            rows, total = fetch_esi_document_rows(page, args, field, year, target_type)
        except Exception as exc:
            for paper in group_papers:
                results[paper.paper_id] = CitationResult(
                    count=None,
                    source="esi-session",
                    pub_year=paper.pub_year,
                    error=f"ESI Indicators query failed for {field} {year}: {exc}",
                )
            continue

        for paper in group_papers:
            match = find_matching_esi_document(paper, rows)
            if match:
                results[paper.paper_id] = CitationResult(
                    count=parse_int(match.get("citations")),
                    source="esi-session",
                    title=match.get("docTitle") or paper.title,
                    doi=normalize_doi(match.get("DOI") or paper.doi),
                    pub_year=paper.pub_year,
                    official_listed=True,
                )
            else:
                label = "Highly Cited Papers" if target_type == "highly_cited" else "Hot Papers"
                official_error = (
                    f"Not listed in official ESI {label} for {field} {year} "
                    f"(checked {total} row(s))."
                )
                fallback = fetch_openalex_citations(
                    paper,
                    timeout=args.timeout,
                    mailto=args.openalex_mailto or os.getenv("OPENALEX_MAILTO"),
                )
                if fallback.count is not None:
                    results[paper.paper_id] = CitationResult(
                        count=fallback.count,
                        source="esi-session+openalex",
                        title=fallback.title or paper.title,
                        doi=fallback.doi or paper.doi,
                        pub_year=paper.pub_year or fallback.pub_year,
                        official_listed=False,
                        openalex_id=fallback.openalex_id,
                        openalex_field=fallback.openalex_field,
                        openalex_filter=fallback.openalex_filter,
                        error=f"{official_error} Citation count is OpenAlex fallback for progress only.",
                    )
                    continue
                results[paper.paper_id] = CitationResult(
                    count=None,
                    source="esi-session",
                    title=paper.title,
                    doi=paper.doi,
                    pub_year=paper.pub_year,
                    official_listed=False,
                    error=f"{official_error} OpenAlex fallback failed: {fallback.error or 'no citation count returned'}.",
                )
    return results


def fetch_wos_profile_citations_for_papers(
    papers: list[Paper],
    args: argparse.Namespace,
    page: Any,
    session_dir: Path,
) -> dict[str, CitationResult]:
    captured_payloads: list[dict[str, Any]] = []

    def capture_publications_response(response: Any) -> None:
        if "/wos-researcher/dashboard/authorship-claim/?" not in response.url:
            return
        if response.status != 200:
            return
        try:
            payload = response.json()
        except Exception:
            return
        if isinstance(payload, dict):
            captured_payloads.append(payload)

    page.on("response", capture_publications_response)
    try:
        page.goto(WOS_PROFILE_PUBLICATIONS_URL, timeout=int(args.timeout * 1000), wait_until="domcontentloaded")
        wait_for_page_idle(page, int(args.timeout * 1000))
        pass_wos_stay_signed_in(page, args.timeout)
        require_logged_in(page, session_dir, "wos_profile_logged_out")
        page.wait_for_timeout(min(8000, max(2500, int(args.timeout * 1000 / 2))))
    finally:
        try:
            page.remove_listener("response", capture_publications_response)
        except Exception:
            pass

    if not captured_payloads:
        save_debug_page(page, session_dir, "wos_profile_publications_not_loaded")
        shared_error = (
            "Could not capture the WoS Researcher Profile Publications list. "
            f"Open {WOS_PROFILE_PUBLICATIONS_URL} in the saved session and make sure the Publications page loads."
        )
        return {
            paper.paper_id: CitationResult(count=None, source="wos-profile", error=shared_error)
            for paper in papers
        }

    rows: list[dict[str, Any]] = []
    total_count: int | None = None
    for payload in captured_payloads:
        rows.extend(wos_profile_publication_items(payload))
        total_count = total_count or try_parse_int(payload.get("count") or payload.get("total"))

    results: dict[str, CitationResult] = {}
    for paper in papers:
        match = find_matching_wos_profile_publication(paper, rows)
        if match:
            count = wos_profile_publication_citation_count(match)
            title = wos_profile_publication_title(match) or paper.title
            doi = normalize_doi(str(wos_profile_publication_value(match, ("doi", "DOI")) or paper.doi))
            results[paper.paper_id] = CitationResult(
                count=count,
                source="wos-profile",
                title=title,
                doi=doi,
                pub_year=paper.pub_year,
                record_id=str(wos_profile_publication_value(match, ("ut", "UT")) or "") or None,
                error=None if count is not None else "Matched the paper in WoS profile, but citationCount was missing.",
            )
            continue

        fallback = fetch_openalex_citations(
            paper,
            timeout=args.timeout,
            mailto=args.openalex_mailto or os.getenv("OPENALEX_MAILTO"),
        )
        search_scope = f"first {len(rows)} profile publication row(s)"
        if total_count is not None and total_count > len(rows):
            search_scope = f"first {len(rows)} of {total_count} profile publication row(s)"
        profile_error = f"Not found in WoS Researcher Profile Publications ({search_scope})."
        if fallback.count is not None:
            results[paper.paper_id] = CitationResult(
                count=fallback.count,
                source="wos-profile+openalex",
                title=fallback.title or paper.title,
                doi=fallback.doi or paper.doi,
                pub_year=paper.pub_year or fallback.pub_year,
                openalex_id=fallback.openalex_id,
                openalex_field=fallback.openalex_field,
                openalex_filter=fallback.openalex_filter,
                error=f"{profile_error} Citation count is OpenAlex fallback, not your WoS profile count.",
            )
        else:
            results[paper.paper_id] = CitationResult(
                count=None,
                source="wos-profile",
                title=paper.title,
                doi=paper.doi,
                pub_year=paper.pub_year,
                error=f"{profile_error} OpenAlex fallback failed: {fallback.error or 'no citation count returned'}.",
            )
    return results


def wos_profile_publication_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("results", "data", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def wos_profile_publication_value(item: Any, names: tuple[str, ...]) -> Any:
    if isinstance(item, dict):
        for name in names:
            if name in item and item[name] not in (None, ""):
                return item[name]
        for value in item.values():
            found = wos_profile_publication_value(value, names)
            if found not in (None, ""):
                return found
    elif isinstance(item, list):
        for value in item:
            found = wos_profile_publication_value(value, names)
            if found not in (None, ""):
                return found
    return None


def wos_profile_publication_title(item: dict[str, Any]) -> str:
    return str(
        wos_profile_publication_value(
            item,
            ("title", "displayTitle", "name", "displayName", "display_name", "ARTICLE_TITLE"),
        )
        or ""
    )


def wos_profile_publication_citation_count(item: dict[str, Any]) -> int | None:
    return try_parse_int(
        wos_profile_publication_value(
            item,
            ("citationCount", "citation_count", "timesCited", "times_cited", "totalCitationCount", "TIMES_CITED"),
        )
    )


def find_matching_wos_profile_publication(paper: Paper, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    paper_doi = doi_match_key(paper.doi)
    if paper_doi:
        for row in rows:
            if doi_match_key(str(wos_profile_publication_value(row, ("doi", "DOI")) or "")) == paper_doi:
                return row

    paper_title = title_match_key(paper.title)
    if not paper_title:
        return None
    for row in rows:
        row_title = title_match_key(wos_profile_publication_title(row))
        if row_title and (paper_title == row_title or paper_title in row_title or row_title in paper_title):
            return row
    return None


def fetch_esi_document_rows(
    page: Any,
    args: argparse.Namespace,
    esi_field: str,
    pub_year: int,
    target_type: str,
) -> tuple[list[dict[str, Any]], int]:
    limit = 1000
    start = 0
    page_number = 1
    rows: list[dict[str, Any]] = []
    total: int | None = None
    seen: set[str] = set()

    while True:
        params = {
            "type": "documents",
            "docType": esi_document_type(target_type),
            "researchField": esi_field,
            "year": str(pub_year),
            "page": str(page_number),
            "start": str(start),
            "limit": str(limit),
            "sort": json.dumps([{"property": "citations", "direction": "DESC"}], separators=(",", ":")),
        }
        payload = fetch_esi_json(page, params, timeout=args.timeout)
        batch = payload.get("data") or []
        if total is None:
            total = parse_esi_total(payload)
        if not batch:
            break

        for row in batch:
            key = str(row.get("articleUT") or row.get("articleId") or row.get("DOI") or row.get("docTitle") or "")
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)

        start += len(batch)
        page_number += 1
        if total is not None and start >= total:
            break
        if len(batch) < limit:
            break
        if page_number > 50:
            raise MonitorError(f"ESI Indicators pagination exceeded 50 pages for {esi_field} {pub_year}")

    return rows, total if total is not None else len(rows)


def fetch_esi_json(page: Any, params: dict[str, str], timeout: float) -> dict[str, Any]:
    url = ESI_INDICATORS_DATA_URL + "?" + urllib.parse.urlencode(params)
    response = page.evaluate(
        """
        async ({ url, timeoutMs }) => {
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), timeoutMs);
          try {
            const response = await fetch(url, { credentials: 'include', signal: controller.signal });
            const text = await response.text();
            return { status: response.status, text };
          } finally {
            clearTimeout(timer);
          }
        }
        """,
        {"url": url, "timeoutMs": int(timeout * 1000)},
    )
    text = (response.get("text") or "").strip()
    if text == "No results found":
        return {"data": [], "fields": ["0"]}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MonitorError(f"ESI Indicators returned non-JSON response: {text[:160]}") from exc


def parse_esi_total(payload: dict[str, Any]) -> int | None:
    fields = payload.get("fields") or []
    if not fields:
        return None
    return parse_int(str(fields[0]))


def esi_document_type(target_type: str) -> str:
    if target_type == "highly_cited":
        return "highlyCited"
    if target_type == "hot_paper":
        return "Hot"
    return "Top"


def esi_field_query_value(value: str) -> str:
    return value.strip().upper()


def doi_match_key(value: str | None) -> str:
    return normalize_doi((value or "").casefold())


def title_match_key(value: str | None) -> str:
    text = (value or "").casefold()
    text = re.sub(r"[^0-9a-z]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def find_matching_esi_document(paper: Paper, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    paper_doi = doi_match_key(paper.doi)
    if paper_doi:
        for row in rows:
            if doi_match_key(row.get("DOI")) == paper_doi:
                return row

    paper_title = title_match_key(paper.title)
    if not paper_title:
        return None
    for row in rows:
        row_title = title_match_key(row.get("docTitle"))
        if row_title and (paper_title == row_title or paper_title in row_title or row_title in paper_title):
            return row
    return None


def pass_wos_stay_signed_in(page: Any, timeout: float) -> None:
    try:
        text = page.locator("body").inner_text(timeout=5000).casefold()
    except Exception:
        return
    if "currently signed in" not in text or "stay signed in" not in text:
        return
    for pattern in ("Stay Signed In", "保持登录"):
        try:
            button = page.get_by_role("button", name=re.compile(pattern, re.I)).first
            if button.is_visible(timeout=2000):
                button.click(timeout=int(timeout * 1000))
                wait_for_page_idle(page, int(timeout * 1000))
                return
        except Exception:
            continue


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "paper")[:80]


def perform_wos_search(page: Any, query: str, timeout: float) -> None:
    selectors = [
        "input[type='search']",
        "input[placeholder*='Search']",
        "textarea[placeholder*='Search']",
        "input",
        "textarea",
    ]
    filled = False
    for selector in selectors:
        locators = page.locator(selector)
        count = locators.count()
        for index in range(min(count, 8)):
            locator = locators.nth(index)
            try:
                if not locator.is_visible(timeout=1000):
                    continue
                locator.fill(query, timeout=int(timeout * 1000))
                filled = True
                break
            except Exception:
                continue
        if filled:
            break
    if not filled:
        raise MonitorError("Could not find a visible Web of Science search input.")

    clicked = False
    for pattern in ("Search", "检索", "搜索"):
        try:
            button = page.get_by_role("button", name=re.compile(pattern, re.I)).first
            if button.is_visible(timeout=1500):
                button.click(timeout=int(timeout * 1000))
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        page.keyboard.press("Enter")
    page.wait_for_load_state("networkidle", timeout=int(timeout * 1000))


def extract_wos_citation_count(text: str) -> int | None:
    patterns = [
        r"Times\s+Cited(?:\s+in\s+Web\s+of\s+Science\s+Core\s+Collection)?\s*[:\n ]+\s*([0-9,]+)",
        r"([0-9,]+)\s+Times\s+Cited",
        r"Citations?\s*[:\n ]+\s*([0-9,]+)",
        r"被引频次\s*[:\n ]+\s*([0-9,]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return parse_int(match.group(1))
    return None


def first_value(row: dict[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        value = row.get(name, "")
        if value:
            return value
    return ""


def normalize_threshold_rows(rows: list[dict[str, str]], source_label: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        esi_field = first_value(row, ("esi_field", "field", "research_field", "category", "subject"))
        pub_year = parse_int(first_value(row, ("pub_year", "publication_year", "year")))
        if not esi_field or pub_year is None:
            continue
        highly_cited = parse_int(
            first_value(
                row,
                (
                    "highly_cited_threshold",
                    "highly_cited",
                    "hcp_threshold",
                    "citation_threshold",
                    "threshold",
                ),
            )
        )
        hot_paper = parse_int(first_value(row, ("hot_paper_threshold", "hot_paper", "hot_threshold")))
        normalized.append(
            {
                "esi_field": esi_field,
                "pub_year": pub_year,
                "highly_cited_threshold": highly_cited,
                "hot_paper_threshold": hot_paper,
                "snapshot_date": first_value(row, ("snapshot_date", "date", "updated_at")) or utc_now_iso()[:10],
                "source": first_value(row, ("source", "threshold_source")) or source_label,
                "threshold_kind": first_value(row, ("threshold_kind", "kind")) or "imported",
                "total_works": first_value(row, ("total_works", "works_count")),
                "rank": first_value(row, ("rank", "percentile_rank")),
                "openalex_filter": row.get("openalex_filter", ""),
                "notes": row.get("notes", ""),
            }
        )
    if not normalized:
        raise MonitorError(f"No usable threshold rows found from {source_label}")
    return normalized


def write_threshold_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=THRESHOLD_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in THRESHOLD_FIELDNAMES})


def download_threshold_rows(url: str, timeout: float) -> list[dict[str, Any]]:
    text = fetch_text(url, timeout=timeout)
    rows = read_csv_text_dicts(text, url)
    return normalize_threshold_rows(rows, "url")


def fetch_wos_threshold_rows(args: argparse.Namespace) -> list[dict[str, Any]]:
    papers = read_papers(Path(args.papers))
    requested_fields = sorted({paper.esi_field for paper in papers if paper.esi_field})
    if not requested_fields:
        raise MonitorError("papers.csv must include esi_field values to scrape ESI thresholds.")

    playwright, context, page = open_wos_context(args)
    session_dir = wos_session_dir(args)
    try:
        navigate_to_esi_thresholds(page, args)
        require_logged_in(page, session_dir, "thresholds_logged_out")
        rows = extract_wos_threshold_rows(page, requested_fields)
        if not rows:
            save_debug_page(page, session_dir, "thresholds_parse_failed")
            raise MonitorError(
                "Could not parse ESI threshold table from the logged-in page. "
                f"Debug page saved under {session_dir / 'debug'}."
            )
        return rows
    finally:
        close_wos_context(playwright, context)


def navigate_to_esi_thresholds(page: Any, args: argparse.Namespace) -> None:
    timeout_ms = int(args.timeout * 1000)
    page.goto(ESI_THRESHOLDS_URL, timeout=timeout_ms, wait_until="domcontentloaded")
    wait_for_page_idle(page, timeout_ms)
    if threshold_page_ready(page):
        return

    for action in ("click-nav", "direct-location", "direct-goto"):
        try:
            if action == "click-nav":
                page.locator("#nav_thresholds a").click(timeout=timeout_ms)
            elif action == "direct-location":
                page.evaluate("() => { window.location.href = 'ThresholdsAction.action'; }")
            else:
                page.goto(ESI_THRESHOLDS_URL, timeout=timeout_ms, wait_until="domcontentloaded")
            wait_for_page_idle(page, timeout_ms)
            if threshold_page_ready(page):
                return
        except Exception:
            continue


def wait_for_page_idle(page: Any, timeout_ms: int) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass
    try:
        page.wait_for_timeout(1500)
    except Exception:
        pass


def threshold_page_ready(page: Any) -> bool:
    try:
        text = page.locator("body").inner_text(timeout=5000).casefold()
    except Exception:
        return False
    return (
        "highly cited thresholds" in text
        or "hot paper thresholds" in text
        or ("citation thresholds" in text and "2016" in text and "2026" in text)
    )


def extract_wos_threshold_rows(page: Any, requested_fields: list[str]) -> list[dict[str, Any]]:
    body_text = page.locator("body").inner_text(timeout=10000)
    tables = page.evaluate(
        """
        () => Array.from(document.querySelectorAll('table')).map(table =>
          Array.from(table.querySelectorAll('tr')).map(row =>
            Array.from(row.children).map(cell => (cell.innerText || '').trim())
          )
        )
        """
    )
    grids = page.evaluate(
        """
        () => Array.from(document.querySelectorAll('.x-grid')).map(grid => ({
          headers: Array.from(grid.querySelectorAll('.x-column-header-text'))
            .map(cell => (cell.innerText || '').trim()),
          rows: Array.from(grid.querySelectorAll('tr.x-grid-row')).map(row =>
            Array.from(row.querySelectorAll('td .x-grid-cell-inner'))
              .map(cell => (cell.innerText || '').trim())
          )
        }))
        """
    )
    candidates = threshold_candidates_from_tables(tables)
    candidates.extend(threshold_candidates_from_grids(grids))
    candidates.extend(threshold_candidates_from_text(body_text))
    requested = {normalize_field_name(field): field for field in requested_fields}
    snapshot_date = esi_dataset_snapshot_date(body_text)
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        field_label = candidate["field"]
        normalized = normalize_field_name(field_label)
        if normalized not in requested:
            continue
        for year, threshold in candidate["values"].items():
            rows.append(
                {
                    "esi_field": requested[normalized],
                    "pub_year": year,
                    "highly_cited_threshold": threshold,
                    "hot_paper_threshold": "",
                    "snapshot_date": snapshot_date,
                    "source": "clarivate_esi_session",
                    "threshold_kind": "official_highly_cited_top_1_percent",
                    "total_works": "",
                    "rank": "",
                    "openalex_filter": "",
                    "notes": f"Scraped from {ESI_THRESHOLDS_URL}",
                }
            )
    return rows


def threshold_candidates_from_text(text: str) -> list[dict[str, Any]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidates: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if normalize_field_name(line) != "research fields":
            continue
        years: list[int] = []
        cursor = index + 1
        while cursor < len(lines):
            value = try_parse_int(lines[cursor])
            if value is None or value < 1900 or value > 2100:
                break
            years.append(value)
            cursor += 1
        if len(years) < 3:
            continue

        while cursor < len(lines):
            label = lines[cursor]
            if normalize_field_name(label).startswith("incites essential science indicators dataset"):
                break
            if try_parse_int(label) is not None:
                cursor += 1
                continue
            values: dict[int, int] = {}
            ok = True
            for offset, year in enumerate(years):
                value_index = cursor + 1 + offset
                if value_index >= len(lines):
                    ok = False
                    break
                value = try_parse_int(lines[value_index])
                if value is None:
                    ok = False
                    break
                values[year] = value
            if ok:
                candidates.append({"field": label, "values": values})
                cursor += 1 + len(years)
            else:
                cursor += 1
    return candidates


def threshold_candidates_from_tables(tables: list[list[list[str]]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for table in tables:
        candidates.extend(threshold_candidates_from_single_table(table))
    candidates.extend(threshold_candidates_from_split_tables(tables))
    return candidates


def threshold_candidates_from_grids(grids: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for grid in grids:
        headers = grid.get("headers") or []
        rows = grid.get("rows") or []
        year_positions = year_columns(headers)
        if len(year_positions) < 3:
            continue
        years = [year for _, year in year_positions]
        positions = [position for position, _ in year_positions]
        for row in rows:
            values = threshold_values_from_row(row, positions, years)
            if len(values) < 3:
                continue
            field_label = field_label_from_row(row, positions[0])
            if field_label:
                candidates.append({"field": field_label, "values": values})
    return candidates


def threshold_candidates_from_single_table(table: list[list[str]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for header_index, row in enumerate(table):
        year_positions = year_columns(row)
        if len(year_positions) < 3:
            continue
        years = [year for _, year in year_positions]
        positions = [position for position, _ in year_positions]
        for data_row in table[header_index + 1 :]:
            values = threshold_values_from_row(data_row, positions, years)
            if len(values) < 2:
                continue
            field_label = field_label_from_row(data_row, positions[0])
            if field_label:
                candidates.append({"field": field_label, "values": values})
    return candidates


def threshold_candidates_from_split_tables(tables: list[list[list[str]]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    numeric_tables: list[tuple[list[int], list[list[str]]]] = []
    label_tables: list[list[str]] = []
    for table in tables:
        for header_index, row in enumerate(table):
            year_positions = year_columns(row)
            if len(year_positions) >= 3:
                years = [year for _, year in year_positions]
                positions = [position for position, _ in year_positions]
                numeric_rows = [data_row for data_row in table[header_index + 1 :] if threshold_values_from_row(data_row, positions, years)]
                if numeric_rows:
                    numeric_tables.append((years, numeric_rows))
        labels = []
        for row in table:
            label = " ".join(cell for cell in row if cell).strip()
            if label and not any(cell.isdigit() for cell in row):
                labels.append(label)
        if labels:
            label_tables.append(labels)

    for years, numeric_rows in numeric_tables:
        for labels in label_tables:
            if len(labels) != len(numeric_rows):
                continue
            for label, data_row in zip(labels, numeric_rows):
                values = threshold_values_from_row(data_row, list(range(len(years))), years)
                if len(values) >= 2:
                    candidates.append({"field": label, "values": values})
    return candidates


def year_columns(row: list[str]) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    for index, cell in enumerate(row):
        value = try_parse_int(cell)
        if value is not None and 1900 <= value <= 2100:
            pairs.append((index, value))
    return pairs


def threshold_values_from_row(row: list[str], positions: list[int], years: list[int]) -> dict[int, int]:
    values: dict[int, int] = {}
    for position, year in zip(positions, years):
        if position >= len(row):
            continue
        value = try_parse_int(row[position])
        if value is not None:
            values[year] = value
    return values


def field_label_from_row(row: list[str], first_year_position: int) -> str:
    leading = [cell for cell in row[:first_year_position] if cell and try_parse_int(cell) is None]
    if leading:
        return " ".join(leading).strip()
    trailing = [cell for cell in row[first_year_position + 1 :] if cell and try_parse_int(cell) is None]
    return " ".join(trailing).strip()


def build_openalex_works_url(params: dict[str, str]) -> str:
    return "https://api.openalex.org/works?" + urllib.parse.urlencode(params)


def openalex_params(mailto: str | None) -> dict[str, str]:
    if mailto:
        return {"mailto": mailto}
    return {}


def resolve_threshold_context(
    paper: Paper,
    timeout: float,
    mailto: str | None,
) -> tuple[str, int, str]:
    citation_result = fetch_openalex_citations(paper, timeout=timeout, mailto=mailto)
    pub_year = paper.pub_year or citation_result.pub_year
    if pub_year is None:
        raise MonitorError(f"Cannot infer publication year for {paper.paper_id}; fill pub_year in papers.csv")

    openalex_filter = (
        paper.openalex_filter
        or citation_result.openalex_filter
        or openalex_filter_from_esi_field(paper.esi_field)
    )
    if not openalex_filter:
        lookup_error = f" OpenAlex lookup said: {citation_result.error}." if citation_result.error else ""
        raise MonitorError(
            f"Cannot infer OpenAlex field for {paper.paper_id}; fill openalex_filter in papers.csv "
            "such as primary_topic.field.id:https://openalex.org/fields/17."
            f"{lookup_error}"
        )
    esi_field = paper.esi_field or citation_result.openalex_field or "OpenAlex Field"
    return esi_field, pub_year, openalex_filter


def estimate_openalex_threshold(
    pub_year: int,
    openalex_filter: str,
    percentile: float,
    timeout: float,
    mailto: str | None,
) -> tuple[int, int, int]:
    if percentile <= 0 or percentile > 1:
        raise MonitorError("--threshold-percentile must be between 0 and 1")

    filters = f"publication_year:{pub_year},{openalex_filter}"
    base_params = {
        "filter": filters,
        "select": "id,cited_by_count",
    }
    base_params.update(openalex_params(mailto))
    count_url = build_openalex_works_url({**base_params, "per-page": "1"})
    count_data = fetch_json(count_url, timeout=timeout)
    total_works = parse_int((count_data.get("meta") or {}).get("count")) or 0
    if total_works <= 0:
        raise MonitorError(f"OpenAlex returned zero works for filter {filters!r}")

    rank = max(1, math.ceil(total_works * percentile))
    top_url = build_openalex_works_url({**base_params, "sort": "cited_by_count:desc", "per-page": "1"})
    top_data = fetch_json(top_url, timeout=timeout)
    top_results = top_data.get("results") or []
    max_citations = parse_int(top_results[0].get("cited_by_count")) if top_results else None
    if max_citations is None:
        raise MonitorError(f"OpenAlex returned no cited_by_count for filter {filters!r}")

    low = 0
    high = max_citations
    while low < high:
        mid = (low + high + 1) // 2
        citation_filter = f"{filters},cited_by_count:>{mid - 1}"
        mid_url = build_openalex_works_url(
            {**base_params, "filter": citation_filter, "per-page": "1"}
        )
        mid_data = fetch_json(mid_url, timeout=timeout)
        count_at_mid = parse_int((mid_data.get("meta") or {}).get("count")) or 0
        if count_at_mid >= rank:
            low = mid
        else:
            high = mid - 1
    threshold = low
    return threshold, total_works, rank


def refresh_thresholds(args: argparse.Namespace) -> list[dict[str, Any]]:
    source = args.threshold_source
    threshold_url = args.threshold_url or os.getenv("ESI_THRESHOLD_URL", "")
    if source == "auto":
        if threshold_url:
            source = "url"
        elif has_non_estimated_thresholds(Path(args.thresholds)):
            print(f"No threshold URL configured; keeping existing non-estimated thresholds in {args.thresholds}")
            return normalize_threshold_rows(read_csv_dicts(Path(args.thresholds)), "local")
        else:
            raise MonitorError(
                "No official threshold source configured. Set ESI_THRESHOLD_URL / --threshold-url, "
                "or explicitly use --threshold-source openalex-estimate for non-official preview values."
            )

    if source == "url":
        if not threshold_url:
            raise MonitorError("Use --threshold-url or ESI_THRESHOLD_URL for --threshold-source url")
        rows = download_threshold_rows(threshold_url, timeout=args.timeout)
        write_threshold_rows(Path(args.thresholds), rows)
        print(f"Refreshed {len(rows)} threshold row(s) from URL into {args.thresholds}")
        return rows

    if source == "wos-session":
        rows = fetch_wos_threshold_rows(args)
        write_threshold_rows(Path(args.thresholds), rows)
        print(f"Refreshed {len(rows)} official ESI threshold row(s) from logged-in session into {args.thresholds}")
        return rows

    if source != "openalex-estimate":
        raise MonitorError(f"Unsupported threshold source: {source}")

    papers = read_papers(Path(args.papers))
    mailto = args.openalex_mailto or os.getenv("OPENALEX_MAILTO")
    snapshot_date = utc_now_iso()[:10]
    rows_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    for paper in papers:
        esi_field, pub_year, field_filter = resolve_threshold_context(paper, timeout=args.timeout, mailto=mailto)
        key = (esi_field.casefold(), pub_year)
        if key in rows_by_key:
            continue
        threshold, total_works, rank = estimate_openalex_threshold(
            pub_year=pub_year,
            openalex_filter=field_filter,
            percentile=args.threshold_percentile,
            timeout=args.timeout,
            mailto=mailto,
        )
        rows_by_key[key] = {
            "esi_field": esi_field,
            "pub_year": pub_year,
            "highly_cited_threshold": threshold,
            "hot_paper_threshold": "",
            "snapshot_date": snapshot_date,
            "source": "openalex_estimate",
            "threshold_kind": f"estimated_top_{args.threshold_percentile:.4f}_by_citations",
            "total_works": total_works,
            "rank": rank,
            "openalex_filter": field_filter,
            "notes": "Estimated from OpenAlex; not official Clarivate ESI/Web of Science baseline.",
        }
        if args.sleep_between_requests > 0:
            time.sleep(args.sleep_between_requests)

    rows = list(rows_by_key.values())
    write_threshold_rows(Path(args.thresholds), rows)
    print(f"Refreshed {len(rows)} OpenAlex-estimated threshold row(s) into {args.thresholds}")
    return rows


def has_non_estimated_thresholds(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        rows = read_csv_dicts(path)
    except MonitorError:
        return False
    for row in rows:
        source = (row.get("source") or "manual").strip().casefold()
        threshold = row.get("highly_cited_threshold") or row.get("hot_paper_threshold")
        if threshold and source != "openalex_estimate":
            return True
    return False


def evaluate_paper(
    paper: Paper,
    citation_result: CitationResult,
    thresholds: dict[tuple[str, int], dict[str, Any]],
    checked_at: str,
) -> CheckResult:
    pub_year = paper.pub_year or citation_result.pub_year
    threshold = None
    threshold_source = None
    threshold_snapshot = None
    citations = citation_result.count
    error = citation_result.error

    if not paper.esi_field:
        status = "missing_field"
    elif pub_year is None:
        status = "missing_pub_year"
    else:
        threshold_row = thresholds.get((paper.esi_field.casefold(), pub_year))
        if not threshold_row:
            status = "missing_threshold"
        else:
            threshold = threshold_row.get(paper.target_type)
            threshold_source = threshold_row.get("source")
            threshold_snapshot = threshold_row.get("snapshot_date")
            if threshold is None:
                status = "missing_threshold"
            elif citation_result.official_listed is True and citations is None:
                status = "reached"
            elif citations is None:
                status = "missing_citations"
            else:
                if threshold_source == "openalex_estimate":
                    status = "estimate_reached" if citations >= threshold else "estimate_below"
                else:
                    status = "reached" if citations >= threshold or citation_result.official_listed is True else "below"

    margin = None if citations is None or threshold is None else citations - threshold
    progress = None if citations is None or not threshold else round(citations / threshold, 4)
    return CheckResult(
        checked_at=checked_at,
        paper_id=paper.paper_id,
        title=paper.title or citation_result.title or "",
        doi=paper.doi,
        esi_field=paper.esi_field,
        pub_year=pub_year,
        target_type=paper.target_type,
        citations=citations,
        threshold=threshold,
        threshold_source=threshold_source,
        threshold_snapshot=threshold_snapshot,
        margin=margin,
        progress=progress,
        status=status,
        citation_source=citation_result.source,
        official_listed=citation_result.official_listed,
        evidence_title=citation_result.title,
        evidence_doi=citation_result.doi,
        evidence_record_id=citation_result.record_id,
        evidence_openalex_id=citation_result.openalex_id,
        error=error,
        notes=paper.notes,
    )


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"papers": {}}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    tmp_path.replace(path)


def result_key(result: CheckResult) -> str:
    return result.paper_id or result.doi or result.title


def crossing_alerts(results: list[CheckResult], state: dict[str, Any], notify_all: bool) -> list[CheckResult]:
    previous = state.get("papers", {})
    alerts: list[CheckResult] = []
    for result in results:
        key = result_key(result)
        previous_status = previous.get(key, {}).get("status")
        if notify_all and result.status == "reached":
            alerts.append(result)
        elif result.status == "reached" and previous_status != "reached":
            alerts.append(result)
    return alerts


def update_state(results: list[CheckResult], state: dict[str, Any]) -> dict[str, Any]:
    state["last_checked_at"] = utc_now_iso()
    papers = state.setdefault("papers", {})
    for result in results:
        key = result_key(result)
        papers[key] = {
            "status": result.status,
            "citations": result.citations,
            "threshold": result.threshold,
            "margin": result.margin,
            "progress": result.progress,
            "checked_at": result.checked_at,
            "target_type": result.target_type,
        }
    return state


def format_alert(result: CheckResult) -> str:
    label = "ESI高被引" if result.target_type == "highly_cited" else "ESI热点论文"
    return (
        f"{label}提醒：{result.title} 已达到阈值。"
        f"引用数 {result.citations}，阈值 {result.threshold}，超出 {result.margin}。"
    )


def post_webhook(url: str, result: CheckResult, webhook_format: str, timeout: float) -> None:
    if webhook_format == "feishu":
        payload: dict[str, Any] = {"msg_type": "text", "content": {"text": format_alert(result)}}
    else:
        payload = {"text": format_alert(result), "result": asdict(result)}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def write_report(path: Path, results: list[CheckResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(asdict(results[0]).keys()) if results else list(CheckResult.__dataclass_fields__.keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for result in results:
            writer.writerow(asdict(result))


def history_row_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("checked_at") or ""), str(row.get("paper_id") or ""))


def append_history_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(CheckResult.__dataclass_fields__.keys())
    existing_keys: set[tuple[str, str]] = set()
    write_header = not path.exists() or path.stat().st_size == 0
    if not write_header:
        existing_rows = read_csv_dicts(path)
        existing_keys = {history_row_key(row) for row in existing_rows}
        missing_fields = [name for name in fieldnames if name not in existing_rows[0]] if existing_rows else []
        if missing_fields:
            raise MonitorError(
                f"{path} uses an older history format and is missing: {', '.join(missing_fields)}"
            )

    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        for row in rows:
            if history_row_key(row) in existing_keys:
                continue
            writer.writerow({name: row.get(name, "") for name in fieldnames})
            existing_keys.add(history_row_key(row))


def archive_report(path: Path, history_path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    append_history_rows(history_path, read_csv_dicts(path))


def read_history(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    return read_csv_dicts(path)


def history_row_progress(row: dict[str, Any]) -> float:
    try:
        return float(row.get("progress") or float("-inf"))
    except (TypeError, ValueError):
        return float("-inf")


def snapshot_day(row: dict[str, Any]) -> str:
    return str(row.get("checked_at") or "unknown")[:10]


def format_history_row(row: dict[str, Any]) -> str:
    citations = row.get("citations") or "?"
    threshold = row.get("threshold") or "?"
    margin_value = try_parse_int(row.get("margin"))
    margin = "?" if margin_value is None else f"{margin_value:+d}"
    pub_year = row.get("pub_year") or "?"
    threshold_snapshot = row.get("threshold_snapshot") or "?"
    checked_at = row.get("checked_at") or "unknown"
    return (
        f"{checked_at} | [{row.get('status') or 'unknown'}] {row.get('paper_id') or '?'} | "
        f"{row.get('title') or '(untitled)'} | publication year {pub_year} | "
        f"citations {citations} | bar {threshold} (ESI snapshot {threshold_snapshot}) | margin {margin}"
    )


def print_historical_review(results: list[CheckResult], history_rows: list[dict[str, Any]]) -> None:
    if not history_rows:
        return
    print("Historical review from saved snapshots:")
    for result in results:
        rows = [row for row in history_rows if str(row.get("paper_id") or "") == result.paper_id]
        if not rows:
            continue
        reached_rows = [row for row in rows if row.get("status") == "reached"]
        best = max(rows, key=history_row_progress)
        best_citations = best.get("citations") or "?"
        best_threshold = best.get("threshold") or "?"
        if reached_rows:
            days = sorted({snapshot_day(row) for row in reached_rows})
            print(
                f"- {result.paper_id} | {result.title} | publication year {result.pub_year or '?'}: "
                f"REACHED in {len(reached_rows)} saved snapshot(s), {days[0]} to {days[-1]}; "
                f"best {best_citations}/{best_threshold} on {snapshot_day(best)}."
            )
        else:
            print(
                f"- {result.paper_id} | {result.title} | publication year {result.pub_year or '?'}: "
                f"not reached in {len(rows)} saved snapshot(s); "
                f"best {best_citations}/{best_threshold} on {snapshot_day(best)}."
            )


def print_summary(results: list[CheckResult], history_rows: list[dict[str, Any]] | None = None) -> None:
    reached = [result for result in results if result.status == "reached"]
    below = [result for result in results if result.status == "below"]
    estimates = [result for result in results if result.status.startswith("estimate_")]
    unresolved = [result for result in results if result.status not in {"reached", "below"} and not result.status.startswith("estimate_")]
    print(
        f"Checked {len(results)} paper(s): {len(reached)} reached, "
        f"{len(below)} below, {len(estimates)} estimate-only, {len(unresolved)} unresolved."
    )
    for result in sorted(results, key=lambda item: (item.status != "reached", item.margin or -10**9), reverse=False):
        citations = "?" if result.citations is None else str(result.citations)
        threshold = "?" if result.threshold is None else str(result.threshold)
        margin = "?" if result.margin is None else f"{result.margin:+d}"
        source_note = " (OpenAlex fallback)" if result.citation_source.endswith("+openalex") else ""
        snapshot_note = f" (ESI snapshot {result.threshold_snapshot})" if result.threshold_snapshot else ""
        print(
            f"- [{result.status}] {result.paper_id} | {result.title} | "
            f"publication year {result.pub_year or '?'} | citations {citations} | "
            f"bar {threshold}{snapshot_note}{source_note} | margin {margin}"
        )
        if result.error:
            print(f"  error: {result.error}")
    if history_rows is not None:
        print_historical_review(results, history_rows)


def show_history(args: argparse.Namespace) -> None:
    history_path = Path(args.history)
    rows = read_history(history_path)
    if not rows:
        raise MonitorError(
            f"No saved history found in {history_path}. Run a non-dry monitoring check first."
        )
    if args.paper_id:
        rows = [row for row in rows if str(row.get("paper_id") or "") == args.paper_id]
    if args.year is not None:
        rows = [row for row in rows if snapshot_day(row).startswith(str(args.year))]
    if not rows:
        filters = []
        if args.paper_id:
            filters.append(f"paper_id={args.paper_id}")
        if args.year is not None:
            filters.append(f"year={args.year}")
        print(f"No saved history matches {', '.join(filters) or 'the requested filters'}.")
        print("Exact past status cannot be reconstructed when no citation/bar snapshot was saved at that time.")
        return

    rows.sort(key=lambda row: (str(row.get("checked_at") or ""), str(row.get("paper_id") or "")))
    print(f"Saved ESI history: {len(rows)} row(s) from {history_path}")
    for row in rows:
        print(f"- {format_history_row(row)}")

    reached_rows = [row for row in rows if row.get("status") == "reached"]
    reached_papers = sorted({str(row.get("paper_id") or "") for row in reached_rows})
    if reached_papers:
        print(f"Ever reached in this selection: {', '.join(reached_papers)}")
    else:
        print("Ever reached in this selection: none")


def run_once(args: argparse.Namespace) -> list[CheckResult]:
    apply_official_mode(args)
    papers = read_papers(Path(args.papers))
    if getattr(args, "refresh_thresholds", False):
        refresh_thresholds(args)
    thresholds = read_thresholds(Path(args.thresholds))
    checked_at = utc_now_iso()
    results: list[CheckResult] = []
    if args.citation_source == "esi-session":
        playwright, context, page = open_wos_context(args)
        session_dir = wos_session_dir(args)
        try:
            citation_results = fetch_esi_session_citations_for_papers(papers, args, page, session_dir)
            for paper in papers:
                citation_result = citation_results.get(
                    paper.paper_id,
                    CitationResult(
                        count=None,
                        source="esi-session",
                        error="internal error: missing ESI lookup result",
                    ),
                )
                results.append(evaluate_paper(paper, citation_result, thresholds, checked_at))
                if args.sleep_between_requests > 0:
                    time.sleep(args.sleep_between_requests)
        finally:
            close_wos_context(playwright, context)
    elif args.citation_source == "wos-session":
        playwright, context, page = open_wos_context(args)
        session_dir = wos_session_dir(args)
        try:
            for paper in papers:
                try:
                    citation_result = fetch_wos_citations_with_page(paper, args, page, session_dir)
                except Exception as exc:
                    save_debug_page(page, session_dir, f"wos_error_{safe_name(paper.paper_id)}")
                    citation_result = CitationResult(
                        count=None,
                        source="wos-session",
                        error=f"WoS browser error: {exc}",
                    )
                results.append(evaluate_paper(paper, citation_result, thresholds, checked_at))
                if args.sleep_between_requests > 0:
                    time.sleep(args.sleep_between_requests)
        finally:
            close_wos_context(playwright, context)
    elif args.citation_source == "wos-profile":
        playwright, context, page = open_wos_context(args)
        session_dir = wos_session_dir(args)
        try:
            citation_results = fetch_wos_profile_citations_for_papers(papers, args, page, session_dir)
            for paper in papers:
                citation_result = citation_results.get(
                    paper.paper_id,
                    CitationResult(
                        count=None,
                        source="wos-profile",
                        error="internal error: missing WoS profile lookup result",
                    ),
                )
                results.append(evaluate_paper(paper, citation_result, thresholds, checked_at))
                if args.sleep_between_requests > 0:
                    time.sleep(args.sleep_between_requests)
        finally:
            close_wos_context(playwright, context)
    else:
        for paper in papers:
            citation_result = get_citation_result(
                paper=paper,
                source_mode=args.citation_source,
                timeout=args.timeout,
                mailto=args.openalex_mailto or os.getenv("OPENALEX_MAILTO"),
                args=args,
            )
            results.append(evaluate_paper(paper, citation_result, thresholds, checked_at))
            if args.sleep_between_requests > 0:
                time.sleep(args.sleep_between_requests)

    report_path = Path(args.report)
    history_path = Path(args.history)
    if not args.dry_run:
        archive_report(report_path, history_path)
        append_history_rows(history_path, [asdict(result) for result in results])
    write_report(report_path, results)
    state_path = Path(args.state)
    state = load_state(state_path)
    alerts = crossing_alerts(results, state, notify_all=args.notify_all)
    if alerts and args.webhook_url:
        for result in alerts:
            post_webhook(args.webhook_url, result, args.webhook_format, timeout=args.timeout)
    if not args.dry_run:
        save_state(state_path, update_state(results, state))
    history_rows = read_history(history_path)
    if args.dry_run:
        history_rows.extend(asdict(result) for result in results)
    print_summary(results, history_rows)
    if alerts:
        print(f"Alerts: {len(alerts)}")
        for result in alerts:
            print(f"- {format_alert(result)}")
    return results


def apply_official_mode(args: argparse.Namespace) -> None:
    if not getattr(args, "official", False):
        return
    args.refresh_thresholds = True
    args.threshold_source = "wos-session"
    args.citation_source = "wos-profile"


def init_files(args: argparse.Namespace) -> None:
    paper_path = Path(args.papers)
    threshold_path = Path(args.thresholds)
    if not paper_path.exists():
        paper_path.write_text(
            "paper_id,title,doi,esi_field,pub_year,manual_citations,target_type,openalex_filter,notes\n"
            "my-paper-1,Replace with your paper title,10.0000/example.doi,Computer Science,2024,,highly_cited,,\n",
            encoding="utf-8",
        )
        print(f"Created {paper_path}")
    else:
        print(f"Exists: {paper_path}")

    if not threshold_path.exists():
        threshold_path.write_text(
            "esi_field,pub_year,highly_cited_threshold,hot_paper_threshold,snapshot_date,source,threshold_kind,total_works,rank,openalex_filter,notes\n"
            "Computer Science,2024,100,20,YYYY-MM-DD,manual,official_or_manual,,,,Replace thresholds with Clarivate ESI Baselines\n",
            encoding="utf-8",
        )
        print(f"Created {threshold_path}")
    else:
        print(f"Exists: {threshold_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor papers against ESI highly-cited or hot-paper thresholds.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common_options(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--papers", default=DEFAULT_PAPERS, help=f"Paper CSV path. Default: {DEFAULT_PAPERS}")
        subparser.add_argument(
            "--thresholds",
            default=DEFAULT_THRESHOLDS,
            help=f"ESI threshold CSV path. Default: {DEFAULT_THRESHOLDS}",
        )

    def add_wos_options(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--wos-session-dir",
            default=DEFAULT_WOS_SESSION_DIR,
            help=f"Persistent browser session directory. Default: {DEFAULT_WOS_SESSION_DIR}",
        )
        subparser.add_argument(
            "--wos-headed",
            action="store_true",
            help="Show the browser while using the WoS/ESI session.",
        )

    def add_run_options(subparser: argparse.ArgumentParser) -> None:
        add_common_options(subparser)
        subparser.add_argument("--state", default=DEFAULT_STATE, help=f"State JSON path. Default: {DEFAULT_STATE}")
        subparser.add_argument("--report", default=DEFAULT_REPORT, help=f"Report CSV path. Default: {DEFAULT_REPORT}")
        subparser.add_argument(
            "--history",
            default=DEFAULT_HISTORY,
            help=f"Append-only monitoring history CSV. Default: {DEFAULT_HISTORY}",
        )
        subparser.add_argument(
            "--citation-source",
            choices=["auto", "openalex", "manual", "wos-session", "esi-session", "wos-profile"],
            default="auto",
            help="auto uses OpenAlex and falls back to manual_citations. Default: auto",
        )
        subparser.add_argument("--timeout", type=float, default=20.0, help="Network timeout in seconds. Default: 20")
        subparser.add_argument(
            "--sleep-between-requests",
            type=float,
            default=0.2,
            help="Delay between OpenAlex requests. Default: 0.2",
        )
        subparser.add_argument("--openalex-mailto", default="", help="Email passed to OpenAlex polite pool.")
        subparser.add_argument("--webhook-url", default=os.getenv("ESI_MONITOR_WEBHOOK_URL", ""))
        subparser.add_argument("--webhook-format", choices=["generic", "feishu"], default="generic")
        subparser.add_argument("--notify-all", action="store_true", help="Notify every reached paper, not only crossings.")
        subparser.add_argument("--dry-run", action="store_true", help="Do not update state.")
        subparser.add_argument(
            "--refresh-thresholds",
            action="store_true",
            help="Refresh threshold CSV before checking.",
        )
        subparser.add_argument(
            "--official",
            action="store_true",
            help="Use logged-in Clarivate ESI thresholds and WoS Researcher Profile citation counts.",
        )
        add_wos_options(subparser)
        add_threshold_refresh_options(subparser)

    def add_threshold_refresh_options(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--threshold-source",
            choices=["auto", "url", "wos-session", "openalex-estimate"],
            default="auto",
            help="auto uses ESI_THRESHOLD_URL when set, otherwise keeps existing official thresholds. Default: auto",
        )
        subparser.add_argument(
            "--threshold-url",
            default=os.getenv("ESI_THRESHOLD_URL", ""),
            help="CSV URL for official/exported thresholds. Can also use ESI_THRESHOLD_URL.",
        )
        subparser.add_argument(
            "--threshold-percentile",
            type=float,
            default=DEFAULT_THRESHOLD_PERCENTILE,
            help="Percentile used for OpenAlex estimate. 0.01 means top 1%%. Default: 0.01",
        )

    init_parser = subparsers.add_parser("init", help="Create sample CSV files if they do not exist.")
    add_common_options(init_parser)
    init_parser.set_defaults(func=init_files)

    login_parser = subparsers.add_parser("login-wos", help="Open a browser to log in to Clarivate/WoS once.")
    add_wos_options(login_parser)
    login_parser.add_argument("--timeout", type=float, default=120.0, help="Browser timeout in seconds. Default: 120")
    login_parser.add_argument("--login-url", default=ESI_THRESHOLDS_URL, help="URL to open for login.")
    login_parser.set_defaults(func=login_wos)

    refresh_parser = subparsers.add_parser("refresh-thresholds", help="Auto-fill threshold CSV from URL or OpenAlex.")
    add_common_options(refresh_parser)
    add_wos_options(refresh_parser)
    add_threshold_refresh_options(refresh_parser)
    refresh_parser.add_argument("--timeout", type=float, default=20.0, help="Network timeout in seconds. Default: 20")
    refresh_parser.add_argument(
        "--sleep-between-requests",
        type=float,
        default=0.2,
        help="Delay between OpenAlex requests. Default: 0.2",
    )
    refresh_parser.add_argument("--openalex-mailto", default="", help="Email passed to OpenAlex polite pool.")
    refresh_parser.set_defaults(func=refresh_thresholds)

    once_parser = subparsers.add_parser("once", help="Run one monitoring check.")
    add_run_options(once_parser)
    once_parser.set_defaults(func=run_once)

    daemon_parser = subparsers.add_parser("daemon", help="Run checks repeatedly.")
    add_run_options(daemon_parser)
    daemon_parser.add_argument(
        "--interval-hours",
        type=float,
        default=DEFAULT_INTERVAL_HOURS,
        help=f"Hours between checks. Default: {DEFAULT_INTERVAL_HOURS}",
    )
    daemon_parser.set_defaults(func=run_daemon)

    history_parser = subparsers.add_parser("history", help="Show saved citation/bar snapshots and past reach status.")
    history_parser.add_argument("--history", default=DEFAULT_HISTORY, help=f"History CSV path. Default: {DEFAULT_HISTORY}")
    history_parser.add_argument("--year", type=int, help="Only show snapshots checked in this calendar year.")
    history_parser.add_argument("--paper-id", default="", help="Only show one paper_id.")
    history_parser.set_defaults(func=show_history)
    return parser


def run_daemon(args: argparse.Namespace) -> None:
    interval_seconds = max(args.interval_hours * 3600, 60)
    while True:
        try:
            run_once(args)
        except Exception as exc:  # noqa: BLE001 - keep the monitor alive.
            print(f"[{utc_now_iso()}] check failed: {exc}", file=sys.stderr)
        print(f"Sleeping {interval_seconds / 3600:.2f} hour(s). Press Ctrl+C to stop.")
        time.sleep(interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("Stopped.")
        return 130
    except MonitorError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
