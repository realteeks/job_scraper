import os
import re
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

app = FastAPI(title="Naukri Selenium Scraper Service")


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def clean_noise(name_text: str) -> str:
    name_text = re.sub(r"\b\d\.\d\b.*$", "", name_text)
    name_text = re.sub(
        r"\s+\d+(\.\d+)?\s*K?\s*Reviews?.*$",
        "",
        name_text,
        flags=re.IGNORECASE,
    )
    return name_text.strip()


def extract_company_name(soup, visible_text: str):
    blocklist = [
        "naukri",
        "info edge",
        "justdial",
        "linkedin",
        "indeed",
        "glassdoor",
    ]
    for selector in [
        "[class*='jd-header-comp-name']",
        "[class*='comp-name']",
        "a[href*='company-jobs']",
        "[class*='company-name']",
    ]:
        for tag in soup.select(selector):
            a_tag = tag.find("a") if hasattr(tag, "find") else None
            text = (
                a_tag.get_text(" ", strip=True)
                if a_tag
                else tag.get_text(" ", strip=True)
            )
            text = clean_text(text)
            if text and not any(
                bad_word in text.lower() for bad_word in blocklist
            ):
                cleaned_name = clean_noise(text)
                if cleaned_name:
                    return cleaned_name

    lines = [line.strip() for line in visible_text.splitlines() if line.strip()]
    for i, line in enumerate(lines):
        if "reviews" in line.lower() and i > 0:
            potential_company = lines[i - 1]
            if not any(
                bad_word in potential_company.lower() for bad_word in blocklist
            ):
                return clean_noise(potential_company)
    return None


def extract_job_description(soup, visible_text: str):
    if visible_text:
        match = re.search(
            r"(?:Job description|Job Description)\s*(.*?)\s*(?:Key Skills|Role details|Disclaimer|About the company|$)",
            visible_text,
            re.DOTALL | re.IGNORECASE,
        )
        if match:
            extracted_text = clean_text(match.group(1))
            if len(extracted_text) > 50:
                return extracted_text

    selectors = [
        "[class*='styles_JDC__']",
        "[class*='dang-inner-html']",
        "[class*='job-desc']",
        "#jobDescriptionText",
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = clean_text(node.get_text("\n", strip=True))
            if len(text) > 50:
                return text
    return None


def run_selenium_scraper(url: str):
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument(
        "--disable-dev-shm-usage"
    )  # Crucial for Docker containers
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument(
        "--disable-software-rasterizer"
    )  # Drops RAM overhead
    chrome_options.add_argument(
        "--blink-settings=imagesEnabled=false"
    )  # Blocks images to save massive RAM
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    driver = webdriver.Chrome(options=chrome_options)
    try:
        driver.get(url)
        WebDriverWait(driver, 35).until(
            EC.presence_of_element_located((By.TAG_NAME, "h1"))
        )
        WebDriverWait(driver, 35).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        buttons = driver.find_elements(
            By.XPATH,
            "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'read more')]",
        )
        for btn in buttons:
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", btn
                )
                btn.click()
            except Exception:
                continue

        driver.implicitly_wait(5)
        html = driver.page_source
        visible_text = driver.find_element(By.TAG_NAME, "body").text
        return html, visible_text
    finally:
        # Guarantee browser closes so Render doesn't run out of memory
        driver.quit()


@app.get("/")
def health_check():
    return {
        "status": "ok",
        "message": "Naukri Selenium Docker Service is running!",
    }


@app.get("/scrape")
def scrape_job(
    url: str = Query(..., description="The full Naukri job URL to scrape"),
):
    try:
        html, visible_text = run_selenium_scraper(url)
        soup = BeautifulSoup(html, "html.parser")

        company = extract_company_name(soup, visible_text)
        jd = extract_job_description(soup, visible_text)

        return {
            "status": "success",
            "url": url,
            "company": company or "Not found",
            "job_description": jd or "Not found",
        }
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Scraping failed: {str(e)}"
        )