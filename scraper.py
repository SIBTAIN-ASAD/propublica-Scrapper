import re
import time
import pandas as pd
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

BASE = "https://projects.propublica.org"
START_URL = "https://projects.propublica.org/nonprofits/search?sort=-recent_annual_revenue&state%5B%5D=CA&recent_annual_revenue%5B%5D=5000000&q=&submit=Apply"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; nonprofit-scraper/1.0)"
}

session = requests.Session()
session.headers.update(HEADERS)


def get_soup(url):
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def get_org_links(search_url):
    links = []
    soup = get_soup(search_url)

    for a in soup.select(".result-item__hed a"):
        href = a.get("href")
        if href:
            links.append(urljoin(BASE, href))

    return links


def get_next_page(search_url):
    soup = get_soup(search_url)
    next_a = soup.find("a", string=re.compile(r"Next", re.I))
    if next_a and next_a.get("href"):
        return urljoin(BASE, next_a["href"])
    return None


def get_latest_filing_url(org_url):
    soup = get_soup(org_url)

    view_btn = soup.find("a", string=re.compile(r"View Filing", re.I))
    if not view_btn:
        return None

    return urljoin(BASE, view_btn["href"])


def get_irs990_iframe_url(filing_url):
    soup = get_soup(filing_url)

    iframe = soup.select_one("#forms-area-border iframe[src*='IRS990']")
    if not iframe:
        return None

    return iframe["src"]


def extract_from_irs990(iframe_url):
    soup = get_soup(iframe_url)
    text = soup.get_text("\n", strip=True)

    data = {
        "Org Name": "",
        "Address": "",
        "Employer ID": "",
        "Phone": "",
        "Email": "",
        "Org Website": "",
    }

    # Most ProPublica rendered IRS990 pages contain labels from XML.
    patterns = {
        "Org Name": [
            r"Name of organization\s+(.+)",
            r"BusinessNameLine1Txt\s+(.+)",
        ],
        "Employer ID": [
            r"Employer identification number\s+([0-9\-]+)",
            r"EIN\s+([0-9\-]+)",
        ],
        "Phone": [
            r"Telephone number\s+([0-9\-\(\)\s\.]+)",
            r"PhoneNum\s+([0-9\-\(\)\s\.]+)",
        ],
        "Email": [
            r"([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})",
        ],
        "Org Website": [
            r"(https?://[^\s]+)",
            r"WebsiteAddressTxt\s+(.+)",
        ],
    }

    for key, regs in patterns.items():
        for reg in regs:
            m = re.search(reg, text, re.I)
            if m:
                data[key] = clean(m.group(1))
                break

    # Address fallback: collect common address XML labels if present
    address_parts = []
    for label in [
        "AddressLine1Txt",
        "AddressLine2Txt",
        "CityNm",
        "StateAbbreviationCd",
        "ZIPCd",
    ]:
        m = re.search(label + r"\s+([^\n]+)", text, re.I)
        if m:
            address_parts.append(clean(m.group(1)))

    if address_parts:
        data["Address"] = ", ".join(address_parts)
    else:
        m = re.search(
            r"Number and street.*?\n(.+?)\n.*?City or town.*?\n(.+?)\n.*?State.*?\n(.+?)\n.*?ZIP code.*?\n(.+)",
            text,
            re.I | re.S,
        )
        if m:
            data["Address"] = clean(", ".join(m.groups()))

    return data


def scrape_all(max_pages=None, delay=1):
    results = []
    page_url = START_URL
    page_count = 0

    while page_url:
        page_count += 1
        print(f"Scraping search page {page_count}: {page_url}")

        org_links = get_org_links(page_url)

        for org_url in org_links:
            print(f"  Org: {org_url}")

            row = {
                "Org Page": org_url,
                "Filing Page": "",
                "IRS990 URL": "",
                "Org Name": "",
                "Address": "",
                "Employer ID": "",
                "Phone": "",
                "Email": "",
                "Org Website": "",
            }

            try:
                filing_url = get_latest_filing_url(org_url)
                row["Filing Page"] = filing_url or ""

                if filing_url:
                    iframe_url = get_irs990_iframe_url(filing_url)
                    row["IRS990 URL"] = iframe_url or ""

                    if iframe_url:
                        row.update(extract_from_irs990(iframe_url))

            except Exception as e:
                row["Error"] = str(e)

            results.append(row)
            time.sleep(delay)

        if max_pages and page_count >= max_pages:
            break

        page_url = get_next_page(page_url)
        time.sleep(delay)

    return results


if __name__ == "__main__":
    data = scrape_all(max_pages=None, delay=1)

    df = pd.DataFrame(data)
    df.to_excel("propublica_nonprofits_ca.xlsx", index=False)

    print(f"Done. Exported {len(df)} rows to propublica_nonprofits_ca.xlsx")