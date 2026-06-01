import re
import time
import json
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

REVENUE_FILTER = "5000000"
DELAY = 1
MAX_ROWS_PER_XLSX = 50000

STATES = [
    "CA", "NY", "TX", "FL", "PA", "IL", "OH", "MA", "VA", "NC",
    "MI", "NJ", "WA", "CO", "DC", "MN", "GA", "MD", "WI", "IN",
    "MO", "TN", "OR", "CT", "AZ", "SC", "IA", "LA", "AL", "KY",
    "OK", "DE", "KS", "UT", "NE", "NV", "NM", "AR", "MS", "ME",
    "WV", "HI", "MT", "VT", "NH", "RI", "ID", "AK", "SD", "ND",
    "WY", "PR", "VI", "GU", "MP", "PW"
]

HEADERS = [
    "State",
    "Page",
    "Org Name",
    "Address",
    "Employer ID",
    "Phone",
    "Email",
    "Org Website",
    "Org URL",
    "Filing URL",
    "IRS990 URL",
    "Error",
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

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(60)
    return driver


class ExcelChunkWriter:
    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.chunk_no = 1
        self.row_count = 0
        self.wb = None
        self.ws = None
        self._new_file()

    def _new_file(self):
        self.wb = Workbook(write_only=True)
        self.ws = self.wb.create_sheet("results")
        self.ws.append(HEADERS)
        self.row_count = 0

    def append(self, row):
        self.ws.append([row.get(h, "") for h in HEADERS])
        self.row_count += 1

        if self.row_count >= MAX_ROWS_PER_XLSX:
            self.save()
            self.chunk_no += 1
            self._new_file()

    def save(self):
        if self.row_count == 0:
            return

        path = self.out_dir / f"propublica_results_part_{self.chunk_no}.xlsx"
        self.wb.save(path)
        print(f"[SAVE] Saved {self.row_count} rows -> {path}")

    def close(self):
        self.save()


def checkpoint(state, page):
    data = {"last_state": state, "last_page": page}
    with open(OUT_DIR / "checkpoint.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_org_links(driver, wait, state, page):
    url = build_search_url(state, page)
    print(f"\n[PAGE] State={state} Page={page}")
    print(f"[URL] {url}")

    driver.get(url)

    time.sleep(2)

    cards = driver.find_elements(By.CSS_SELECTOR, ".result-item__hed a")

    if not cards:
        print(f"[EMPTY] No results found for State={state} Page={page}")
        return []

    links = []
    for a in cards:
        href = a.get_attribute("href")
        if href:
            links.append(href)

    print(f"[FOUND] {len(links)} organizations")
    return links


def find_after(lines, labels):
    for label in labels:
        for i, line in enumerate(lines):
            if label.lower() in line.lower():
                for j in range(i + 1, min(i + 5, len(lines))):
                    value = lines[j].strip()
                    if value and value.lower() != label.lower():
                        return value
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
        "Org URL": org_url,
        "Filing URL": "",
        "IRS990 URL": "",
        "Org Name": "",
        "Address": "",
        "Employer ID": "",
        "Phone": "",
        "Email": "",
        "Org Website": "",
        "Error": "",
    }

    try:
        print(f"[ORG] {org_url}")
        driver.get(org_url)

        view_filing = wait.until(
            EC.presence_of_element_located((By.LINK_TEXT, "View Filing"))
        )

        filing_url = view_filing.get_attribute("href")
        row["Filing URL"] = filing_url
        print(f"[FILING] {filing_url}")

        driver.get(filing_url)

        iframe = wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "#forms-area-border iframe[src*='IRS990']")
            )
        )

        row["IRS990 URL"] = iframe.get_attribute("src")
        print(f"[IFRAME] {row['IRS990 URL']}")

        driver.switch_to.frame(iframe)

        body = wait.until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        text = body.text
        driver.switch_to.default_content()

        lines = [x.strip() for x in text.splitlines() if x.strip()]

        row["Org Name"] = find_after(lines, [
            "Name of organization",
            "Business Name",
            "BusinessNameLine1Txt",
        ])

        row["Employer ID"] = find_after(lines, [
            "Employer identification number",
            "EIN",
        ])

        row["Phone"] = find_after(lines, [
            "Telephone number",
            "Phone",
            "PhoneNum",
        ])

        row["Org Website"] = find_after(lines, [
            "Website",
            "WebsiteAddressTxt",
        ]) or extract_website(text)

        row["Email"] = extract_email(text)

        address_1 = find_after(lines, [
            "Number and street",
            "AddressLine1Txt",
        ])
        city = find_after(lines, [
            "City or town",
            "CityNm",
        ])
        state_line = find_after(lines, [
            "State",
            "StateAbbreviationCd",
        ])
        zip_code = find_after(lines, [
            "ZIP code",
            "ZIPCd",
        ])

        row["Address"] = ", ".join(
            x for x in [address_1, city, state_line, zip_code] if x
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
    driver = setup_driver()
    wait = WebDriverWait(driver, 25)
    writer = ExcelChunkWriter(OUT_DIR)

    total = 0

    try:
        for state in STATES:
            page = 1
            print(f"\n========== START STATE {state} ==========")

            while True:
                checkpoint(state, page)

                org_links = get_org_links(driver, wait, state, page)

                if not org_links:
                    print(f"[STATE DONE] {state} ended at page {page}")
                    break

                for org_url in org_links:
                    row = scrape_org(driver, wait, org_url, state, page)
                    writer.append(row)
                    total += 1

                    print(f"[TOTAL] {total} rows collected")
                    time.sleep(DELAY)

                page += 1
                time.sleep(DELAY)

            print(f"========== END STATE {state} ==========")

    finally:
        writer.close()
        driver.quit()
        print(f"\n[DONE] Total rows scraped: {total}")
        print(f"[OUTPUT] Files saved in: {OUT_DIR.resolve()}")


if __name__ == "__main__":
    main()