import re
import time
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

from openpyxl import Workbook
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


BASE = "https://projects.propublica.org"
OUT_DIR = Path("propublica_output")
OUT_DIR.mkdir(exist_ok=True)

DB_PATH = OUT_DIR / "propublica_scrape.sqlite"
CHECKPOINT_PATH = OUT_DIR / "checkpoint.json"
EXCEL_PATH = OUT_DIR / "propublica_all_states.xlsx"

REVENUE_FILTER = "5000000"
DELAY = 1
EXPORT_EVERY_ROWS = 500

STATES = [
    "CA", "NY", "TX", "FL", "PA", "IL", "OH", "MA", "VA", "NC",
    "MI", "NJ", "WA", "CO", "DC", "MN", "GA", "MD", "WI", "IN",
    "MO", "TN", "OR", "CT", "AZ", "SC", "IA", "LA", "AL", "KY",
    "OK", "DE", "KS", "UT", "NE", "NV", "NM", "AR", "MS", "ME",
    "WV", "HI", "MT", "VT", "NH", "RI", "ID", "AK", "SD", "ND",
    "WY", "PR", "VI", "GU", "MP", "PW"
]

HEADERS = [
    "State", "Page", "Org Name", "Address", "Employer ID", "Phone",
    "Email", "Org Website", "Org URL", "Filing URL", "IRS990 URL", "Error"
]


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


def setup_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1600,1200")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    return webdriver.Chrome(options=options)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
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
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM results WHERE org_url = ?", (org_url,))
    return cur.fetchone() is not None


def save_row(conn, row):
    cur = conn.cursor()

    cur.execute("""
        INSERT OR REPLACE INTO results (
            org_url, state, page, org_name, address, employer_id, phone,
            email, org_website, filing_url, irs990_url, error
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


def save_checkpoint(state, page):
    CHECKPOINT_PATH.write_text(
        json.dumps({"state": state, "page": page}, indent=2),
        encoding="utf-8"
    )


def load_checkpoint():
    if not CHECKPOINT_PATH.exists():
        return None

    return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))


def export_excel(conn):
    print("[EXPORT] Creating Excel file...")

    wb = Workbook(write_only=True)
    cur = conn.cursor()

    for state in STATES:
        ws = wb.create_sheet(title=state)
        ws.append(HEADERS)

        cur.execute("""
            SELECT
                state, page, org_name, address, employer_id, phone,
                email, org_website, org_url, filing_url, irs990_url, error
            FROM results
            WHERE state = ?
            ORDER BY page, org_url
        """, (state,))

        count = 0
        for row in cur.fetchall():
            ws.append(row)
            count += 1

        print(f"[EXPORT] {state}: {count} rows")

    wb.save(EXCEL_PATH)
    print(f"[EXPORT DONE] {EXCEL_PATH}")


def get_org_links(driver, state, page):
    url = build_search_url(state, page)

    print(f"\n[PAGE] State={state} Page={page}")
    print(f"[URL] {url}")

    driver.get(url)
    time.sleep(2)

    links = [
        a.get_attribute("href")
        for a in driver.find_elements(By.CSS_SELECTOR, ".result-item__hed a")
        if a.get_attribute("href")
    ]

    print(f"[FOUND] {len(links)} firms")

    return links


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


def scrape_org(driver, wait, org_url, state, page):
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
        print(f"[ORG] {org_url}")
        driver.get(org_url)

        btn = wait.until(
            EC.presence_of_element_located((By.LINK_TEXT, "View Filing"))
        )

        filing_url = btn.get_attribute("href")
        row["Filing URL"] = filing_url

        driver.get(filing_url)

        iframe = wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "#forms-area-border iframe[src*='IRS990']")
            )
        )

        row["IRS990 URL"] = iframe.get_attribute("src")

        driver.switch_to.frame(iframe)

        body = wait.until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        text = body.text
        driver.switch_to.default_content()

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

        address_1 = find_after(lines, ["Number and street", "AddressLine1Txt"])
        city = find_after(lines, ["City or town", "CityNm"])
        state_code = find_after(lines, ["State", "StateAbbreviationCd"])
        zip_code = find_after(lines, ["ZIP code", "ZIPCd"])

        row["Address"] = ", ".join(
            x for x in [address_1, city, state_code, zip_code] if x
        )

        print(f"[OK] {row['Org Name']} | EIN={row['Employer ID']}")

    except Exception as e:
        row["Error"] = str(e)
        print(f"[ERROR] {org_url} -> {e}")

        try:
            driver.switch_to.default_content()
        except Exception:
            pass

    return row


def main():
    resume = input("Resume from last checkpoint? y/n: ").strip().lower() == "y"
    checkpoint = load_checkpoint() if resume else None

    start_state = checkpoint["state"] if checkpoint else STATES[0]
    start_page = checkpoint["page"] if checkpoint else 1

    conn = init_db()
    driver = setup_driver()
    wait = WebDriverWait(driver, 25)

    total_new = 0
    should_start = not resume

    try:
        for state in STATES:
            if resume and not should_start:
                if state == start_state:
                    should_start = True
                else:
                    continue

            page = start_page if state == start_state else 1

            print(f"\n========== START STATE {state} ==========")

            while True:
                save_checkpoint(state, page)

                org_links = get_org_links(driver, state, page)

                if not org_links:
                    print(f"[STATE DONE] {state} ended at page {page}")
                    break

                for org_url in org_links:
                    if already_scraped(conn, org_url):
                        print(f"[SKIP] Already scraped: {org_url}")
                        continue

                    row = scrape_org(driver, wait, org_url, state, page)
                    save_row(conn, row)

                    total_new += 1
                    print(f"[SAVED] Total new rows this run: {total_new}")

                    if total_new % EXPORT_EVERY_ROWS == 0:
                        export_excel(conn)

                    time.sleep(DELAY)

                page += 1
                time.sleep(DELAY)

            start_page = 1

        export_excel(conn)

    finally:
        driver.quit()
        conn.close()
        print("[DONE]")


if __name__ == "__main__":
    main()