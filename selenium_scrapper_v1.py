from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import pandas as pd
import time

START_URL = "https://projects.propublica.org/nonprofits/search?sort=-recent_annual_revenue&state%5B%5D=CA&recent_annual_revenue%5B%5D=5000000&q=&submit=Apply"

options = Options()
# options.add_argument("--headless=new")
options.add_argument("--disable-gpu")

driver = webdriver.Chrome(options=options)
wait = WebDriverWait(driver, 20)

results = []

driver.get(START_URL)

firm_links = [
    a.get_attribute("href")
    for a in wait.until(
        EC.presence_of_all_elements_located(
            (By.CSS_SELECTOR, ".result-item__hed a")
        )
    )
]

for firm_url in firm_links:
    row = {
        "Org Name": "",
        "Address": "",
        "Employer ID": "",
        "Phone": "",
        "Email": "",
        "Org Website": "",
        "Firm URL": firm_url,
        "Filing URL": "",
    }

    driver.get(firm_url)

    view_filing = wait.until(
        EC.element_to_be_clickable((By.LINK_TEXT, "View Filing"))
    )
    filing_url = view_filing.get_attribute("href")
    row["Filing URL"] = filing_url

    driver.get(filing_url)

    iframe = wait.until(
        EC.presence_of_element_located(
            (By.CSS_SELECTOR, "#forms-area-border iframe[src*='IRS990']")
        )
    )

    driver.switch_to.frame(iframe)

    text = driver.find_element(By.TAG_NAME, "body").text

    driver.switch_to.default_content()

    # Basic extraction helpers
    lines = [x.strip() for x in text.splitlines() if x.strip()]

    def find_after(label):
        for i, line in enumerate(lines):
            if label.lower() in line.lower() and i + 1 < len(lines):
                return lines[i + 1]
        return ""

    row["Org Name"] = find_after("Name of organization")
    row["Employer ID"] = find_after("Employer identification number")
    row["Phone"] = find_after("Telephone number")
    row["Org Website"] = find_after("Website")

    # Email fallback
    import re
    email_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", text)
    if email_match:
        row["Email"] = email_match.group(0)

    # Address rough fallback
    address_1 = find_after("Number and street")
    city = find_after("City or town")
    state = find_after("State")
    zip_code = find_after("ZIP code")

    row["Address"] = ", ".join(
        x for x in [address_1, city, state, zip_code] if x
    )

    results.append(row)
    time.sleep(1)

driver.quit()

df = pd.DataFrame(results)
df.to_excel("propublica_nonprofits.xlsx", index=False)

print("Done: propublica_nonprofits.xlsx")