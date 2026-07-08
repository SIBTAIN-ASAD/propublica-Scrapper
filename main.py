import re
import time
import json
import queue
import sqlite3
import threading
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin
import random

import requests
import urllib3
from bs4 import BeautifulSoup
from requests.exceptions import ChunkedEncodingError, ContentDecodingError


BASE = "https://projects.propublica.org"
OUT_DIR = Path("propublica_output")
OUT_DIR.mkdir(exist_ok=True)

DB_PATH = OUT_DIR / "propublica_scrape.sqlite"
CHECKPOINT_PATH = OUT_DIR / "checkpoint.json"

REVENUE_FILTER = "5000000"

WORKERS = 4
DELAY = 1.5
PAGE_DELAY = 20

REQUEST_TIMEOUT = 45
LIST_RETRIES = 8
MAX_QUEUE_SIZE = 500
'''
curl -k -x https://unblock.oxylabs.io:60000 \
-U 'pucitdata_v8I7V:Z7as3Mq9Uam~' \
'https://ip.oxylabs.io/location'
'''
# OXYLABS_USER = "sam1122_q67R1"
# OXYLABS_PASS = "~YdFfGT=g6B3=JU+"
OXYLABS_USER = "pucitdata_v8I7V"
OXYLABS_PASS = "Z7as3Mq9Uam~"

OXYLABS_PROXY_HOST = "unblock.oxylabs.io:60000"

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

STATES = [
    "CA", "NY", "TX", "FL", "PA", "IL", "OH", "MA", "VA", "NC",
    "MI", "NJ", "WA", "CO", "DC", "MN", "GA", "MD", "WI", "IN",
    "MO", "TN", "OR", "CT", "AZ", "SC", "IA", "LA", "AL", "KY",
    "OK", "DE", "KS", "UT", "NE", "NV", "NM", "AR", "MS", "ME",
    "WV", "HI", "MT", "VT", "NH", "RI", "ID", "AK", "SD", "ND",
    "WY", "PR", "VI", "GU", "MP", "PW"
]

db_lock = threading.Lock()
print_lock = threading.Lock()
task_queue = queue.Queue(maxsize=MAX_QUEUE_SIZE)

session = requests.Session()

session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/149.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Referer": "https://projects.propublica.org/nonprofits/",
})



def oxylabs_proxies():
    user = quote(OXYLABS_USER, safe="")
    passwd = quote(OXYLABS_PASS, safe="")
    proxy_url = f"http://{user}:{passwd}@{OXYLABS_PROXY_HOST}"
    return {"http": proxy_url, "https": proxy_url}


def oxylabs_get(url, timeout=REQUEST_TIMEOUT):
    return session.get(
        url,
        timeout=timeout,
        proxies=oxylabs_proxies(),
        verify=False,
    )


def direct_get(url, timeout=REQUEST_TIMEOUT):
    return session.get(url, timeout=timeout)


def parse_list_html(html):
    soup = BeautifulSoup(html, "html.parser")

    links = []
    for a in soup.select(".result-item__hed a"):
        href = a.get("href")
        if href:
            links.append(urljoin(BASE, href))

    page_text = soup.get_text(" ", strip=True).lower()
    has_search_structure = (
        "nonprofit explorer" in page_text
        or "search results" in page_text
        or "filter" in page_text
        or bool(soup.select_one("form"))
        or bool(soup.select_one(".result-item"))
    )

    return links, has_search_structure


def fetch_list_page(url, use_oxylabs=False):
    if use_oxylabs:
        response = oxylabs_get(url)
        via = "Oxylabs"
    else:
        response = direct_get(url)
        via = "direct"
    return response, via


FORM_TYPES = ("IRS990", "IRS990PF", "IRS990EZ")
WARNING_PREFIX = "WARNING:"


def form_type_from_url(url):
    for form in FORM_TYPES:
        if url.rstrip("/").endswith("/" + form):
            return form
    return url.rstrip("/").split("/")[-1]


def is_warning_error(error):
    return bool(error and error.startswith(WARNING_PREFIX))


def is_garbled_response(text):
    if not text:
        return True

    sample = text[:3000]
    lower = sample.lower()
    if any(
        marker in lower
        for marker in ("<html", "<!doctype", "nonprofit", "businessnamelinetxt")
    ):
        return False

    non_text = sum(1 for ch in sample if ord(ch) < 32 and ch not in "\r\n\t")
    return non_text > max(20, len(sample) * 0.08)


def read_response_text(response):
    try:
        text = response.text
    except (ContentDecodingError, ChunkedEncodingError, UnicodeDecodeError) as e:
        raise ValueError(f"Response decode failed: {e}") from e

    if is_garbled_response(text):
        raise ValueError("Garbled/binary response body")

    return text


def fetch_with_fallback(url, timeout=REQUEST_TIMEOUT, label="", validate=None):
    response = direct_get(url, timeout)
    via = "direct"

    if response.status_code == 404:
        return response, via

    if response.status_code in (429, 403, 500, 502, 503, 504):
        log(f"[FETCH] {label} HTTP {response.status_code} via direct, retrying Oxylabs")
        response = oxylabs_get(url, timeout)
        return response, "Oxylabs"

    if response.status_code == 200:
        try:
            text = read_response_text(response)
        except ValueError as e:
            log(f"[FETCH] {label} {e}, retrying Oxylabs")
            response = oxylabs_get(url, timeout)
            return response, "Oxylabs"

        incomplete = validate(text) if validate else len(text) < 15000
        if incomplete:
            reason = "incomplete content" if validate else f"blocked response ({len(text)} bytes)"
            log(f"[FETCH] {label} {reason}, retrying Oxylabs")
            response = oxylabs_get(url, timeout)
            via = "Oxylabs"

    return response, via


def get_view_filing_url(html):
    soup = BeautifulSoup(html, "html.parser")

    for a in soup.select('a[href*="/full"]'):
        if re.search(r"view filing", a.get_text(" ", strip=True), re.I):
            return urljoin(BASE, a["href"])

    for a in soup.select('a.btn[href*="/full"], .btns a[href*="/full"]'):
        return urljoin(BASE, a["href"])

    match = re.search(r'href="(/nonprofits/organizations/\d+/\d+/full)"', html)
    if match:
        return urljoin(BASE, match.group(1))

    return None


def has_filing_content(html, filing_url):
    return bool(get_filing_form_urls(html, filing_url))


def has_irs990_content(html):
    return (
        "BusinessNameLine1Txt" in html
        or "Name of organization" in html
        or "Name of foundation" in html
        or "Employer identification number" in html
    )


def get_filing_form_urls(html, filing_url):
    urls = []
    seen = set()

    soup = BeautifulSoup(html, "html.parser")
    for iframe in soup.select("#forms-area iframe[src], #forms-area-border iframe[src]"):
        src = (iframe.get("src") or "").strip()
        if not src or "full_text" not in src:
            continue
        url = urljoin(BASE, src)
        if url not in seen:
            seen.add(url)
            urls.append(url)

    match = re.search(r"/organizations/\d+/(\d+)/full", filing_url)
    if match:
        filing_id = match.group(1)
        for form in FORM_TYPES:
            url = f"{BASE}/nonprofits/full_text/{filing_id}/{form}"
            if url not in seen:
                seen.add(url)
                urls.append(url)

    return sorted(urls, key=lambda url: (
        FORM_TYPES.index(form_type_from_url(url))
        if form_type_from_url(url) in FORM_TYPES
        else len(FORM_TYPES)
    ))


def get_irs990_url(html, filing_url):
    soup = BeautifulSoup(html, "html.parser")
    link = soup.select_one("a#IRS990")
    if link and link.get("src"):
        return urljoin(BASE, link["src"])

    match = re.search(r"/organizations/\d+/(\d+)/full", filing_url)
    if match:
        return f"{BASE}/nonprofits/full_text/{match.group(1)}/IRS990"

    if filing_url.endswith("/full"):
        return filing_url.replace("/full", "/IRS990")

    return None


def sleep_random(min_seconds=2, max_seconds=6):
    delay = random.uniform(min_seconds, max_seconds)
    time.sleep(delay)
    

def log(msg):
    with print_lock:
        print(msg, flush=True)


def build_search_url(state, page):
    params = {
        "sort": "-recent_annual_revenue",
        "state[]": state,
        "recent_annual_revenue[]": REVENUE_FILTER,
        "q": "",
        "submit": "Apply",
        "page": page,
    }
    return f"{BASE}/nonprofits/search?{urlencode(params, doseq=True)}"


def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS results (
            org_url TEXT PRIMARY KEY,
            state TEXT,
            page INTEGER,
            org_name TEXT,
            address TEXT,
            employer_id TEXT,
            phone TEXT,
            email TEXT,
            org_website TEXT,
            filing_url TEXT,
            irs990_url TEXT,
            error TEXT,
            scraped_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    return conn


def already_scraped(conn, org_url):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT 1 FROM results
            WHERE org_url = ?
              AND (
                error IS NULL
                OR error = ''
                OR error LIKE 'WARNING:%'
              )
            """,
            (org_url,),
        )
        return cur.fetchone() is not None


def save_row(conn, row):
    with db_lock:
        conn.execute("""
            INSERT OR REPLACE INTO results (
                org_url,
                state,
                page,
                org_name,
                address,
                employer_id,
                phone,
                email,
                org_website,
                filing_url,
                irs990_url,
                error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row["Org URL"],
            row["State"],
            row["Page"],
            row["Org Name"],
            row["Address"],
            row["Employer ID"],
            row["Phone"],
            row["Email"],
            row["Org Website"],
            row["Filing URL"],
            row["IRS990 URL"],
            row["Error"],
        ))

        conn.commit()


def get_db_stats(conn):
    with db_lock:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM results")
        total = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM results WHERE error IS NULL OR error = ''"
        )
        ok_count = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM results WHERE error LIKE 'WARNING:%'"
        )
        warned_count = cur.fetchone()[0]

    return {
        "total": total,
        "ok": ok_count,
        "warned": warned_count,
        "errored": total - ok_count - warned_count,
    }


def get_errored_rows(conn):
    with db_lock:
        cur = conn.cursor()
        cur.execute("""
            SELECT org_url, state, page, error
            FROM results
            WHERE error IS NOT NULL
              AND error != ''
              AND error NOT LIKE 'WARNING:%'
            ORDER BY state, page, org_url
        """)
        return cur.fetchall()


def get_warned_rows(conn):
    with db_lock:
        cur = conn.cursor()
        cur.execute("""
            SELECT org_url, state, page, error
            FROM results
            WHERE error LIKE 'WARNING:%'
            ORDER BY state, page, org_url
        """)
        return cur.fetchall()


def print_db_stats(conn):
    stats = get_db_stats(conn)
    checkpoint = load_checkpoint()

    log("\n=== Database Stats ===")
    log(f"Total rows:    {stats['total']}")
    log(f"Successful:    {stats['ok']}")
    log(f"Warnings:      {stats['warned']}")
    log(f"Errored:       {stats['errored']}")

    if checkpoint:
        log(
            f"Checkpoint:    state={checkpoint['state']} "
            f"page={checkpoint['page']}"
        )
    else:
        log("Checkpoint:    none")

    if stats["errored"]:
        log("\nRecent errors:")
        for org_url, state, page, error in get_errored_rows(conn)[:5]:
            preview = (error or "")[:100]
            log(f"  - {org_url} [{state} p{page}]: {preview}")
        if stats["errored"] > 5:
            log(f"  ... and {stats['errored'] - 5} more")

    if stats["warned"]:
        log("\nRecent warnings:")
        for org_url, state, page, error in get_warned_rows(conn)[:5]:
            preview = (error or "")[:100]
            log(f"  - {org_url} [{state} p{page}]: {preview}")
        if stats["warned"] > 5:
            log(f"  ... and {stats['warned'] - 5} more")


def show_start_menu(conn):
    stats = get_db_stats(conn)
    checkpoint = load_checkpoint()

    print("\n=== ProPublica Scraper ===")
    print(f"Database: {DB_PATH}")
    print(
        f"Rows: {stats['total']} total | "
        f"{stats['ok']} ok | {stats['warned']} warned | {stats['errored']} errored"
    )
    if checkpoint:
        print(
            f"Checkpoint: state={checkpoint['state']} "
            f"page={checkpoint['page']}"
        )
    print()
    print("1. Fresh start (scrape from beginning)")
    print("2. Resume from checkpoint")
    print("3. Retry errored rows only")
    print("4. Retry warning rows only")
    print("5. Show database stats")
    print("6. Exit")
    print()

    while True:
        choice = input("Select option [1-6]: ").strip()
        if choice in {"1", "2", "3", "4", "5", "6"}:
            return choice
        print("Invalid choice. Enter 1, 2, 3, 4, 5, or 6.")


def save_checkpoint(state, page):
    with db_lock:
        CHECKPOINT_PATH.write_text(
            json.dumps({"state": state, "page": page}, indent=2),
            encoding="utf-8"
        )


def load_checkpoint():
    if CHECKPOINT_PATH.exists():
        return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    return None


def get_org_links(state, page):
    """
    Uses requests instead of Selenium for listing pages.
    Returns:
        links: list[str]
        ok: bool

    ok=False means page was not confirmed and should be retried.
    ok=True + links=[] means verified empty page, so state pagination can stop.
    """

    url = build_search_url(state, page)

    log(f"\n[LIST PAGE] State={state} Page={page}")
    log(f"[URL] {url}")

    last_error = None

    for attempt in range(1, LIST_RETRIES + 1):
        try:
            sleep_random(3, 8)

            response, via = fetch_list_page(url, use_oxylabs=False)

            log(
                f"[LIST HTTP] State={state} Page={page} "
                f"Attempt={attempt}/{LIST_RETRIES} "
                f"Status={response.status_code} "
                f"Bytes={len(response.text)} "
                f"Via={via}"
            )

            if response.status_code == 429:
                wait_time = min(300, 30 * attempt)
                last_error = "HTTP 429 Too Many Requests"

                log(
                    f"[RATE LIMITED] State={state} Page={page}. "
                    f"Sleeping {wait_time}s, then retrying via Oxylabs."
                )

                time.sleep(wait_time)

                response, via = fetch_list_page(url, use_oxylabs=True)
                log(
                    f"[LIST HTTP] State={state} Page={page} "
                    f"Attempt={attempt}/{LIST_RETRIES} "
                    f"Status={response.status_code} "
                    f"Bytes={len(response.text)} "
                    f"Via={via}"
                )

            elif response.status_code in (403, 500, 502, 503, 504):
                last_error = f"HTTP {response.status_code}"

                log(
                    f"[HTTP RETRY] State={state} Page={page} "
                    f"Status={response.status_code}. Retrying via Oxylabs."
                )

                response, via = fetch_list_page(url, use_oxylabs=True)
                log(
                    f"[LIST HTTP] State={state} Page={page} "
                    f"Attempt={attempt}/{LIST_RETRIES} "
                    f"Status={response.status_code} "
                    f"Bytes={len(response.text)} "
                    f"Via={via}"
                )

            if response.status_code == 429:
                wait_time = min(300, 30 * attempt)
                last_error = "HTTP 429 Too Many Requests (via Oxylabs)"

                log(
                    f"[RATE LIMITED] State={state} Page={page}. "
                    f"Sleeping {wait_time}s before retry."
                )

                time.sleep(wait_time)
                continue

            if response.status_code in [403, 500, 502, 503, 504]:
                wait_time = min(180, 20 * attempt)
                last_error = f"HTTP {response.status_code}"

                log(
                    f"[HTTP RETRY] State={state} Page={page} "
                    f"Status={response.status_code}. Sleeping {wait_time}s."
                )

                time.sleep(wait_time)
                continue

            response.raise_for_status()

            try:
                list_html = read_response_text(response)
            except ValueError as e:
                last_error = str(e)
                log(f"[LIST GARBLED] State={state} Page={page}: {last_error}")
                time.sleep(20 * attempt)
                continue

            links, has_search_structure = parse_list_html(list_html)

            if links:
                log(f"[LIST FOUND] State={state} Page={page} Firms={len(links)}")
                return links, True

            if has_search_structure:
                log(f"[LIST EMPTY VERIFIED] State={state} Page={page}")
                return [], True

            if via == "direct":
                last_error = "Blocked or empty response from direct fetch"
                log(
                    f"[LIST BLOCKED] State={state} Page={page}: {last_error}. "
                    f"Retrying via Oxylabs."
                )

                response, via = fetch_list_page(url, use_oxylabs=True)
                log(
                    f"[LIST HTTP] State={state} Page={page} "
                    f"Attempt={attempt}/{LIST_RETRIES} "
                    f"Status={response.status_code} "
                    f"Bytes={len(response.text)} "
                    f"Via={via}"
                )

                if response.status_code == 200:
                    response.raise_for_status()
                    try:
                        list_html = read_response_text(response)
                    except ValueError:
                        list_html = ""
                    links, has_search_structure = parse_list_html(list_html)

                    if links:
                        log(f"[LIST FOUND] State={state} Page={page} Firms={len(links)}")
                        return links, True

                    if has_search_structure:
                        log(f"[LIST EMPTY VERIFIED] State={state} Page={page}")
                        return [], True

            last_error = "No firm links and no recognizable search-page structure"
            log(f"[LIST SUSPICIOUS EMPTY] State={state} Page={page}: {last_error}")
            time.sleep(20 * attempt)

        except Exception as e:
            last_error = str(e)

            log(
                f"[LIST ERROR] State={state} Page={page} "
                f"Attempt={attempt}/{LIST_RETRIES} -> {e}."
            )

            try:
                response, via = fetch_list_page(url, use_oxylabs=True)
                log(
                    f"[LIST HTTP] State={state} Page={page} "
                    f"Attempt={attempt}/{LIST_RETRIES} "
                    f"Status={response.status_code} "
                    f"Bytes={len(response.text)} "
                    f"Via={via} (fallback after error)"
                )

                if response.status_code == 200:
                    response.raise_for_status()
                    try:
                        list_html = read_response_text(response)
                    except ValueError:
                        list_html = ""
                    links, has_search_structure = parse_list_html(list_html)

                    if links:
                        log(f"[LIST FOUND] State={state} Page={page} Firms={len(links)}")
                        return links, True

                    if has_search_structure:
                        log(f"[LIST EMPTY VERIFIED] State={state} Page={page}")
                        return [], True
            except Exception as oxylabs_error:
                last_error = f"{e}; Oxylabs fallback failed: {oxylabs_error}"

            wait_time = min(180, 20 * attempt)
            log(f"Sleeping {wait_time}s.")
            time.sleep(wait_time)

    log(f"[LIST FAILED] State={state} Page={page} Error={last_error}")
    return [], False



def find_after(lines, labels):
    for label in labels:
        for i, line in enumerate(lines):
            if label.lower() in line.lower():
                for j in range(i + 1, min(i + 6, len(lines))):
                    val = lines[j].strip()
                    if val:
                        return val
    return ""


def extract_email(text):
    m = re.search(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", text, re.I)
    return m.group(0) if m else ""


def extract_website(text):
    m = re.search(r"https?://[^\s]+|www\.[^\s]+", text, re.I)
    return m.group(0) if m else ""


def fill_row_from_irs990_text(row, text):
    lines = [x.strip() for x in text.splitlines() if x.strip()]

    row["Org Name"] = find_after(lines, [
        "Name of organization",
        "Name of foundation",
        "BusinessNameLine1Txt",
    ])

    row["Employer ID"] = find_after(lines, [
        "Employer identification number",
        "EIN",
    ])

    row["Phone"] = find_after(lines, [
        "Telephone number",
        "PhoneNum",
    ])

    row["Email"] = extract_email(text)

    row["Org Website"] = find_after(lines, [
        "Website",
        "WebsiteAddressTxt",
    ]) or extract_website(text)

    address_1 = find_after(lines, [
        "Number and street",
        "AddressLine1Txt",
    ])
    city = find_after(lines, [
        "City or town",
        "CityNm",
    ])
    state_code = find_after(lines, [
        "State",
        "StateAbbreviationCd",
    ])
    zip_code = find_after(lines, [
        "ZIP code",
        "ZIPCd",
    ])

    row["Address"] = ", ".join(
        x for x in [address_1, city, state_code, zip_code] if x
    )


def scrape_org(org_url, state, page, worker_id):
    row = {
        "State": state,
        "Page": page,
        "Org Name": "",
        "Address": "",
        "Employer ID": "",
        "Phone": "",
        "Email": "",
        "Org Website": "",
        "Org URL": org_url,
        "Filing URL": "",
        "IRS990 URL": "",
        "Error": "",
    }

    try:
        log(f"[W{worker_id}] ORG {org_url}")

        response, via = fetch_with_fallback(
            org_url,
            label=f"W{worker_id} org",
            validate=lambda html: get_view_filing_url(html) is None,
        )
        log(
            f"[W{worker_id}] ORG page status={response.status_code} "
            f"bytes={len(response.text)} via={via}"
        )
        response.raise_for_status()
        org_html = read_response_text(response)

        filing_url = get_view_filing_url(org_html)
        if not filing_url:
            raise Exception("View Filing link not found")
        row["Filing URL"] = filing_url

        response, via = fetch_with_fallback(
            filing_url,
            label=f"W{worker_id} filing",
            validate=lambda html: not has_filing_content(html, filing_url),
        )
        log(
            f"[W{worker_id}] Filing page status={response.status_code} "
            f"bytes={len(response.content)} via={via}"
        )
        response.raise_for_status()
        filing_html = read_response_text(response)
        primary_irs990_url = get_irs990_url(filing_html, filing_url)

        form_urls = []
        if primary_irs990_url:
            form_urls.append(primary_irs990_url)
        for url in get_filing_form_urls(filing_html, filing_url):
            if url not in form_urls:
                form_urls.append(url)

        if not form_urls:
            raise Exception("No tax form URLs found on filing page")

        text = None
        used_url = None
        warning = ""

        for form_url in form_urls:
            form_name = form_type_from_url(form_url)
            form_response, via = fetch_with_fallback(
                form_url,
                label=f"W{worker_id} {form_name}",
                validate=lambda html: not has_irs990_content(html),
            )
            log(
                f"[W{worker_id}] Form {form_name} status={form_response.status_code} "
                f"bytes={len(form_response.text)} via={via}"
            )

            if form_response.status_code == 404:
                log(f"[W{worker_id}] Form 404, trying next: {form_url}")
                continue

            if form_response.status_code != 200:
                continue

            try:
                form_html = read_response_text(form_response)
            except ValueError:
                log(f"[W{worker_id}] Form garbled, trying next: {form_url}")
                continue

            if not has_irs990_content(form_html):
                continue

            text = BeautifulSoup(form_html, "html.parser").get_text(
                "\n", strip=True
            )
            if not text:
                continue

            used_url = form_url
            if form_url != primary_irs990_url:
                warning = (
                    f"{WARNING_PREFIX} primary IRS990 unavailable; "
                    f"used {form_name} from filing page"
                )
            break

        if not text or not used_url:
            raise Exception("No usable tax form found on filing page")

        row["IRS990 URL"] = used_url
        fill_row_from_irs990_text(row, text)

        if warning:
            row["Error"] = warning
            log(
                f"[W{worker_id}] OK (warning) {row['Org Name']} | "
                f"EIN={row['Employer ID']} | {warning}"
            )
        else:
            log(f"[W{worker_id}] OK {row['Org Name']} | EIN={row['Employer ID']}")

    except Exception as e:
        row["Error"] = str(e)
        log(f"[W{worker_id}] ERROR {org_url} -> {e}")

    return row


def make_error_row(state, page, org_url, error):
    return {
        "State": state,
        "Page": page,
        "Org Name": "",
        "Address": "",
        "Employer ID": "",
        "Phone": "",
        "Email": "",
        "Org Website": "",
        "Org URL": org_url,
        "Filing URL": "",
        "IRS990 URL": "",
        "Error": str(error),
    }


def worker(worker_id, conn):
    while True:
        item = task_queue.get()

        if item is None:
            task_queue.task_done()
            break

        if len(item) == 4:
            state, page, org_url, force_retry = item
        else:
            state, page, org_url = item
            force_retry = False

        try:
            if force_retry:
                log(f"[W{worker_id}] RETRY {org_url}")
                row = scrape_org(org_url, state, page, worker_id)
                save_row(conn, row)
                if is_warning_error(row["Error"]):
                    log(f"[W{worker_id}] WARNED {org_url}")
                elif row["Error"]:
                    log(f"[W{worker_id}] STILL ERRORED {org_url}")
                else:
                    log(f"[W{worker_id}] FIXED {org_url}")
            elif already_scraped(conn, org_url):
                log(f"[W{worker_id}] SKIP already scraped {org_url}")
            else:
                row = scrape_org(org_url, state, page, worker_id)
                save_row(conn, row)
                if is_warning_error(row["Error"]):
                    log(f"[W{worker_id}] SAVED (warning) {org_url}")
                else:
                    log(f"[W{worker_id}] SAVED {org_url}")

            time.sleep(DELAY)

        except Exception as e:
            log(f"[W{worker_id}] UNEXPECTED ERROR {org_url}: {e}")
            save_row(conn, make_error_row(state, page, org_url, e))

        finally:
            task_queue.task_done()

    log(f"[W{worker_id}] stopped")


def producer(conn, resume=False):
    checkpoint = load_checkpoint() if resume else None

    start_state = checkpoint["state"] if checkpoint else STATES[0]
    start_page = checkpoint["page"] if checkpoint else 1

    should_start = not resume

    for state in STATES:
        if resume and not should_start:
            if state == start_state:
                should_start = True
            else:
                continue

        page = start_page if state == start_state else 1

        log(f"\n========== PRODUCER START STATE {state} ==========")

        while True:
            save_checkpoint(state, page)

            links, ok = get_org_links(state, page)

            if not ok:
                log(
                    f"[PAGE NOT CONFIRMED] State={state} Page={page}. "
                    f"Retrying same page after delay."
                )
                time.sleep(120)
                continue

            if not links:
                log(f"[STATE DONE] {state} ended at page {page}")
                break

            added = 0

            for org_url in links:
                if not already_scraped(conn, org_url):
                    task_queue.put((state, page, org_url))
                    added += 1
                else:
                    log(f"[PRODUCER SKIP] Already scraped {org_url}")

            log(f"[QUEUE] Added={added} Pending={task_queue.qsize()}")

            page += 1
            time.sleep(PAGE_DELAY)

        start_page = 1
        log(f"========== PRODUCER END STATE {state} ==========")


def retry_rows_producer(conn, rows, label):
    if not rows:
        log(f"[RETRY] No {label} rows found in database.")
        return

    log(f"\n========== RETRY {label.upper()} ROWS ({len(rows)}) ==========")

    for org_url, state, page, error in rows:
        preview = (error or "")[:80]
        log(f"[RETRY QUEUE] {org_url} | previous: {preview}")
        task_queue.put((state or "", page or 0, org_url, True))

    log(f"[RETRY] Queued {len(rows)} orgs for retry")


def retry_errors_producer(conn):
    retry_rows_producer(conn, get_errored_rows(conn), "errored")


def retry_warnings_producer(conn):
    retry_rows_producer(conn, get_warned_rows(conn), "warning")


def start_workers(conn):
    threads = []

    log(f"[START] Workers={WORKERS}")
    log(f"[DB] {DB_PATH}")

    for i in range(WORKERS):
        t = threading.Thread(
            target=worker,
            args=(i + 1, conn),
            daemon=True,
        )
        t.start()
        threads.append(t)

    return threads


def stop_workers(threads):
    for _ in threads:
        task_queue.put(None)

    for t in threads:
        t.join()


def run_scrape(conn, resume=False):
    threads = start_workers(conn)

    try:
        producer(conn, resume=resume)
        log("[PRODUCER DONE] Waiting for worker queue to finish...")
        task_queue.join()
    except KeyboardInterrupt:
        log("[STOP] KeyboardInterrupt received. Waiting for current tasks to stop.")
    finally:
        stop_workers(threads)


def run_retry(conn, producer_fn, label):
    threads = start_workers(conn)

    try:
        producer_fn(conn)
        log(f"[RETRY DONE] Waiting for worker queue to finish...")
        task_queue.join()

        stats = get_db_stats(conn)
        log(
            f"[{label} SUMMARY] ok={stats['ok']} warned={stats['warned']} "
            f"errored={stats['errored']} total={stats['total']}"
        )
    except KeyboardInterrupt:
        log("[STOP] KeyboardInterrupt received. Waiting for current tasks to stop.")
    finally:
        stop_workers(threads)


def run_retry_errors(conn):
    run_retry(conn, retry_errors_producer, "RETRY ERRORS")


def run_retry_warnings(conn):
    run_retry(conn, retry_warnings_producer, "RETRY WARNINGS")


def main():
    conn = init_db()

    while True:
        choice = show_start_menu(conn)

        if choice == "1":
            run_scrape(conn, resume=False)
            log("[DONE] Scraping finished or safely stopped.")
        elif choice == "2":
            if not load_checkpoint():
                log("[WARN] No checkpoint found. Starting fresh instead.")
            run_scrape(conn, resume=True)
            log("[DONE] Scraping finished or safely stopped.")
        elif choice == "3":
            run_retry_errors(conn)
            log("[DONE] Errored retry run finished or safely stopped.")
        elif choice == "4":
            run_retry_warnings(conn)
            log("[DONE] Warning retry run finished or safely stopped.")
        elif choice == "5":
            print_db_stats(conn)
            input("\nPress Enter to return to menu...")
            continue
        elif choice == "6":
            log("[EXIT] Goodbye.")
            break

    conn.close()


if __name__ == "__main__":
    main()