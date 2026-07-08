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
    "Accept-Encoding": "gzip, deflate, br",
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


def fetch_with_fallback(url, timeout=REQUEST_TIMEOUT, label="", validate=None):
    response = direct_get(url, timeout)
    via = "direct"

    if response.status_code in (429, 403, 500, 502, 503, 504):
        log(f"[FETCH] {label} HTTP {response.status_code} via direct, retrying Oxylabs")
        response = oxylabs_get(url, timeout)
        return response, "Oxylabs"

    if response.status_code == 200:
        incomplete = validate(response.text) if validate else len(response.text) < 15000
        if incomplete:
            reason = "incomplete content" if validate else f"blocked response ({len(response.text)} bytes)"
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
    return get_irs990_url(html, filing_url) is not None


def has_irs990_content(html):
    return (
        "BusinessNameLine1Txt" in html
        or "Name of organization" in html
        or "Employer identification number" in html
    )


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
            "SELECT 1 FROM results WHERE org_url = ? AND (error IS NULL OR error = '')",
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

    return {
        "total": total,
        "ok": ok_count,
        "errored": total - ok_count,
    }


def get_errored_rows(conn):
    with db_lock:
        cur = conn.cursor()
        cur.execute("""
            SELECT org_url, state, page, error
            FROM results
            WHERE error IS NOT NULL AND error != ''
            ORDER BY state, page, org_url
        """)
        return cur.fetchall()


def print_db_stats(conn):
    stats = get_db_stats(conn)
    checkpoint = load_checkpoint()

    log("\n=== Database Stats ===")
    log(f"Total rows:    {stats['total']}")
    log(f"Successful:    {stats['ok']}")
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


def show_start_menu(conn):
    stats = get_db_stats(conn)
    checkpoint = load_checkpoint()

    print("\n=== ProPublica Scraper ===")
    print(f"Database: {DB_PATH}")
    print(
        f"Rows: {stats['total']} total | "
        f"{stats['ok']} ok | {stats['errored']} errored"
    )
    if checkpoint:
        print(
            f"Checkpoint: state={checkpoint['state']} "
            f"page={checkpoint['page']}"
        )
    print()
    print("1. Fresh start (scrape from beginning)")
    print("2. Resume from checkpoint")
    print("3. Retry all errored rows in database")
    print("4. Show database stats")
    print("5. Exit")
    print()

    while True:
        choice = input("Select option [1-5]: ").strip()
        if choice in {"1", "2", "3", "4", "5"}:
            return choice
        print("Invalid choice. Enter 1, 2, 3, 4, or 5.")


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

            links, has_search_structure = parse_list_html(response.text)

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
                    links, has_search_structure = parse_list_html(response.text)

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
                    links, has_search_structure = parse_list_html(response.text)

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

        filing_url = get_view_filing_url(response.text)
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
            f"bytes={len(response.text)} via={via}"
        )
        response.raise_for_status()

        irs990_url = get_irs990_url(response.text, filing_url)
        if not irs990_url:
            raise Exception("IRS990 URL not found on filing page")
        row["IRS990 URL"] = irs990_url

        response, via = fetch_with_fallback(
            irs990_url,
            label=f"W{worker_id} IRS990",
            validate=lambda html: not has_irs990_content(html),
        )
        log(
            f"[W{worker_id}] IRS990 status={response.status_code} "
            f"bytes={len(response.text)} via={via}"
        )
        response.raise_for_status()

        text = BeautifulSoup(response.text, "html.parser").get_text("\n", strip=True)
        if not text:
            raise Exception("Empty IRS990 content")

        fill_row_from_irs990_text(row, text)

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
                if row["Error"]:
                    log(f"[W{worker_id}] STILL ERRORED {org_url}")
                else:
                    log(f"[W{worker_id}] FIXED {org_url}")
            elif already_scraped(conn, org_url):
                log(f"[W{worker_id}] SKIP already scraped {org_url}")
            else:
                row = scrape_org(org_url, state, page, worker_id)
                save_row(conn, row)
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


def retry_errors_producer(conn):
    rows = get_errored_rows(conn)

    if not rows:
        log("[RETRY] No errored rows found in database.")
        return

    log(f"\n========== RETRY ERRORED ROWS ({len(rows)}) ==========")

    for org_url, state, page, error in rows:
        preview = (error or "")[:80]
        log(f"[RETRY QUEUE] {org_url} | previous: {preview}")
        task_queue.put((state or "", page or 0, org_url, True))

    log(f"[RETRY] Queued {len(rows)} orgs for retry")


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


def run_retry_errors(conn):
    threads = start_workers(conn)

    try:
        retry_errors_producer(conn)
        log("[RETRY DONE] Waiting for worker queue to finish...")
        task_queue.join()

        stats = get_db_stats(conn)
        log(
            f"[RETRY SUMMARY] ok={stats['ok']} "
            f"errored={stats['errored']} total={stats['total']}"
        )
    except KeyboardInterrupt:
        log("[STOP] KeyboardInterrupt received. Waiting for current tasks to stop.")
    finally:
        stop_workers(threads)


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
            log("[DONE] Retry run finished or safely stopped.")
        elif choice == "4":
            print_db_stats(conn)
            input("\nPress Enter to return to menu...")
            continue
        elif choice == "5":
            log("[EXIT] Goodbye.")
            break

    conn.close()


if __name__ == "__main__":
    main()