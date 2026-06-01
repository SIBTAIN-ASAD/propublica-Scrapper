"""
ProPublica Nonprofit Scraper
Scrapes nonprofit data from ProPublica's nonprofit explorer and exports to Excel.
"""

import requests
from bs4 import BeautifulSoup
import pandas as pd
import time
import re
import logging
from urllib.parse import urljoin

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

BASE_URL = "https://projects.propublica.org"
SEARCH_URL = (
    "https://projects.propublica.org/nonprofits/search"
    "?sort=-recent_annual_revenue&state%5B%5D=CA"
    "&recent_annual_revenue%5B%5D=5000000&q=&submit=Apply"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def get_soup(url: str, retries: int = 3) -> BeautifulSoup | None:
    for attempt in range(retries):
        try:
            resp = SESSION.get(url, timeout=30)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            log.warning(f"Attempt {attempt + 1} failed for {url}: {e}")
            time.sleep(2 ** attempt)
    log.error(f"All retries failed for {url}")
    return None


def get_org_links(search_url: str) -> list[str]:
    """Return all org page hrefs from the search results page."""
    soup = get_soup(search_url)
    if not soup:
        return []
    links = []
    for tag in soup.select("div.result-item__hed a"):
        href = tag.get("href", "")
        if href:
            links.append(urljoin(BASE_URL, href))
    log.info(f"Found {len(links)} organizations")
    return links


def get_filing_url(org_url: str) -> str | None:
    """Find the 'View Filing' button URL on an org page."""
    soup = get_soup(org_url)
    if not soup:
        return None
    btn = soup.find("a", class_="btn", string=re.compile(r"View Filing", re.I))
    if not btn:
        # fallback: first .btn with /full in href
        for a in soup.select("a.btn"):
            href = a.get("href", "")
            if "/full" in href:
                btn = a
                break
    if btn:
        return urljoin(BASE_URL, btn["href"])
    return None


def get_form990_iframe_url(filing_url: str) -> str | None:
    """Extract the IRS990 iframe src from the filing page."""
    soup = get_soup(filing_url)
    if not soup:
        return None
    forms_area = soup.find(id="forms-area-border")
    if not forms_area:
        return None
    iframe = forms_area.find("iframe", id=re.compile(r"^IRS990$"))
    if not iframe:
        iframe = forms_area.find("iframe")
    if iframe:
        src = iframe.get("src", "")
        return urljoin(BASE_URL, src) if src else None
    return None


def extract_text_after_label(soup: BeautifulSoup, label_pattern: str) -> str:
    """Generic helper: find a cell/span that matches label, return adjacent value text."""
    pattern = re.compile(label_pattern, re.I)
    el = soup.find(string=pattern)
    if el:
        parent = el.find_parent()
        if parent:
            nxt = parent.find_next_sibling()
            if nxt:
                return nxt.get_text(strip=True)
            # try parent's next sibling text
            return parent.get_text(strip=True)
    return ""


def parse_form990(form_url: str) -> dict:
    """Parse key fields from the IRS 990 XML-rendered page."""
    soup = get_soup(form_url)
    if not soup:
        return {}

    text = soup.get_text(separator="\n")

    def after_label(patterns: list[str]) -> str:
        for pat in patterns:
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if re.search(pat, line, re.I):
                    # Look ahead up to 3 lines for a non-empty value
                    for j in range(i + 1, min(i + 4, len(lines))):
                        val = lines[j].strip()
                        if val and not re.search(pat, val, re.I):
                            return val
        return ""

    # Try to parse structured table cells (XML renders as tables)
    def find_in_table(label_patterns: list[str]) -> str:
        for pat in label_patterns:
            cells = soup.find_all(string=re.compile(pat, re.I))
            for cell in cells:
                # Try next td/th/span sibling chain
                p = cell.find_parent(["td", "th", "span", "div"])
                if p:
                    nxt = p.find_next_sibling()
                    if nxt:
                        val = nxt.get_text(strip=True)
                        if val:
                            return val
        return ""

    org_name = (
        find_in_table(["Name of organization"])
        or after_label(["Name of organization", "Organization name"])
    )
    address_line1 = (
        find_in_table(["Number and street", "Address"])
        or after_label(["Number and street"])
    )
    city_state_zip = (
        find_in_table(["City or town, state or province"])
        or after_label(["City or town, state"])
    )
    address = f"{address_line1}, {city_state_zip}".strip(", ") if (address_line1 or city_state_zip) else ""

    ein = (
        find_in_table(["Employer identification number", "EIN"])
        or after_label(["Employer identification number", "EIN"])
    )
    phone = (
        find_in_table(["Telephone number", "Phone"])
        or after_label(["Telephone number", "Phone number"])
    )
    email = ""
    # Email is often not on 990 but try
    email_match = re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", text)
    if email_match:
        email = email_match.group(0)

    website = (
        find_in_table(["Website address", "Website"])
        or after_label(["Website address"])
    )
    # Fallback: find URL pattern in text
    if not website:
        url_match = re.search(r"(https?://[^\s,\"<>]+|www\.[^\s,\"<>]+)", text)
        if url_match:
            website = url_match.group(0)

    return {
        "Org Name": org_name,
        "Address": address,
        "Employer ID": ein,
        "Phone": phone,
        "Email": email,
        "Website": website,
    }


def scrape_all(search_url: str) -> list[dict]:
    results = []
    org_links = get_org_links(search_url)

    for i, org_url in enumerate(org_links, 1):
        log.info(f"[{i}/{len(org_links)}] Processing: {org_url}")
        record = {"Org URL": org_url}

        filing_url = get_filing_url(org_url)
        if not filing_url:
            log.warning(f"  No filing found for {org_url}")
            results.append(record)
            continue

        log.info(f"  Filing URL: {filing_url}")
        form_url = get_form990_iframe_url(filing_url)
        if not form_url:
            log.warning(f"  No Form 990 iframe found at {filing_url}")
            results.append(record)
            continue

        log.info(f"  Form 990 URL: {form_url}")
        data = parse_form990(form_url)
        record.update(data)
        results.append(record)

        time.sleep(1.5)  # polite delay

    return results


def save_to_excel(records: list[dict], output_path: str = "nonprofits_data.xlsx"):
    df = pd.DataFrame(records)
    cols = ["Org Name", "Address", "Employer ID", "Phone", "Email", "Website", "Org URL"]
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    df = df[cols]

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Nonprofits"

    header_font = Font(name="Arial", bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", start_color="2E4057")
    center = Alignment(horizontal="center", vertical="center")

    for col_idx, col_name in enumerate(df.columns, 1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center

    alt_fill = PatternFill("solid", start_color="EEF2F7")
    normal_font = Font(name="Arial", size=10)

    for row_idx, row in enumerate(df.itertuples(index=False), 2):
        fill = alt_fill if row_idx % 2 == 0 else None
        for col_idx, val in enumerate(row, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=str(val) if pd.notna(val) else "")
            cell.font = normal_font
            if fill:
                cell.fill = fill

    col_widths = [35, 45, 20, 20, 35, 40, 55]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    wb.save(output_path)
    log.info(f"Saved {len(df)} records to {output_path}")


if __name__ == "__main__":
    records = scrape_all(SEARCH_URL)
    save_to_excel(records, "/mnt/user-data/outputs/nonprofits_data.xlsx")
    print(f"\nDone! Scraped {len(records)} organizations.")
    print("Output saved to: nonprofits_data.xlsx")
