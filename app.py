import logging
import os
import re
import subprocess
import sys

from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from seleniumwire import webdriver  # Changed from selenium to seleniumwire
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("naukri-scraper")

app = FastAPI(title="Naukri Selenium Scraper Service")

# These match the ENV vars baked into the Dockerfile. Selenium does NOT read
# CHROME_BIN / CHROMEDRIVER_PATH automatically -- they have to be wired in
# explicitly below, otherwise Selenium Manager silently takes over and tries
# to locate/download its own browser instead of using the apt-installed one.
CHROME_BIN = os.getenv("CHROME_BIN", "/usr/bin/chromium")
CHROMEDRIVER_PATH = os.getenv("CHROMEDRIVER_PATH", "/usr/bin/chromedriver")


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

    # --- FIX 1: point Selenium at the apt-installed Chromium explicitly. ---
    # Without this, Selenium Manager ignores CHROME_BIN and tries to find/download
    # its own browser, which is a common source of unexplained native crashes
    # (empty "Message:" + raw address stacktrace) in minimal Docker images.
    chrome_options.binary_location = CHROME_BIN

    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-software-rasterizer")
    chrome_options.add_argument("--blink-settings=imagesEnabled=false")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    # --- FIX 4: quiet down Chrome's own background chatter (GCM checkin,
    # component updates, Safe Browsing pings, etc). None of this has
    # anything to do with the page being scraped, but selenium-wire proxies
    # it right alongside everything else -- burning proxy bandwidth/time on
    # every request and cluttering the logs (this is where the
    # android.clients.google.com/checkin lines in your logs come from).
    chrome_options.add_argument("--disable-background-networking")
    chrome_options.add_argument("--disable-component-update")
    chrome_options.add_argument("--disable-sync")
    chrome_options.add_argument("--disable-domain-reliability")
    chrome_options.add_argument("--disable-client-side-phishing-detection")
    chrome_options.add_argument("--no-first-run")

    # CRITICAL: Prevent Chrome from blocking MITM proxy SSL certificates
    chrome_options.add_argument("--ignore-certificate-errors")
    chrome_options.add_argument("--ignore-ssl-errors=yes")

    # Anti-detect automation flags
    chrome_options.add_argument(
        "--disable-blink-features=AutomationControlled"
    )
    chrome_options.add_experimental_option(
        "excludeSwitches", ["enable-automation"]
    )
    chrome_options.add_experimental_option("useAutomationExtension", False)

    scrape_do_token = os.getenv("SCRAPE_DO_TOKEN", "")
    seleniumwire_options = {}

    if scrape_do_token:
        logger.info("Routing Chrome via Scrape.do Indian Residential Proxy Mode...")
        # Format: http://username:password@host:port
        # Scrape.do receives parameters (super=true&geoCode=in) in the password field!
        proxy_url = f"http://{scrape_do_token}:super=true&geoCode=in@proxy.scrape.do:8080"
        seleniumwire_options = {
            "proxy": {
                "http": proxy_url,
                "https": proxy_url,
                "no_proxy": "localhost,127.0.0.1",
                # --- FIX 2: matches Scrape.do's own documented selenium-wire
                # example. Without this, selenium-wire's local proxy can fail
                # to negotiate TLS to the upstream Scrape.do proxy -- this is
                # separate from the --ignore-certificate-errors Chrome flag
                # above, which only covers Chrome's own cert checking, not
                # selenium-wire's internal connection to the upstream proxy.
                "verify_ssl": False,
            }
        }
    else:
        logger.warning("SCRAPE_DO_TOKEN not set. Connecting directly...")

    # --- FIX 3: explicit Service pointing at the apt-installed chromedriver,
    # with its own stdout/stderr streamed to our logs. This is the single
    # biggest diagnostic upgrade: chromedriver's *own* log (real Chrome exit
    # reason, missing-library errors, etc.) will now show up in Render's log
    # viewer instead of being swallowed, so any future crash is legible.
    service = Service(
        executable_path=CHROMEDRIVER_PATH,
        log_output=sys.stdout,
    )

    driver = None
    try:
        driver = webdriver.Chrome(
            service=service,
            options=chrome_options,
            seleniumwire_options=seleniumwire_options,
        )

        # Navigate directly to the real Naukri URL (NOT the API url)
        driver.get(url)

        try:
            WebDriverWait(driver, 35).until(
                EC.presence_of_element_located((By.TAG_NAME, "h1"))
            )
        except TimeoutException:
            # --- FIX 5: on timeout, log exactly what Chrome was actually
            # looking at instead of letting a bare, useless TimeoutException
            # bubble up. current_url tells us if we got redirected (e.g. to
            # a login/verification/interstitial page); title + a text
            # snippet tell us if we landed on a CAPTCHA / "unusual traffic"
            # / proxy-error page instead of the real job listing. Without
            # this, every timeout looks identical whether the cause is
            # blocking, a dead proxy, or something else entirely -- this is
            # the single most useful thing to add before guessing further.
            try:
                diag_url = driver.current_url
                diag_title = driver.title
                diag_snippet = clean_text(
                    driver.find_element(By.TAG_NAME, "body").text
                )[:500]
            except Exception as diag_err:
                diag_url = diag_title = diag_snippet = f"<diagnostics failed: {diag_err}>"
            logger.error(
                "h1 never appeared within 35s. current_url=%s title=%r body_snippet=%r",
                diag_url,
                diag_title,
                diag_snippet,
            )
            raise

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
        if driver is not None:
            driver.quit()


def _binary_version(path: str) -> str:
    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=10
        )
        output = (result.stdout or result.stderr).strip()
        return output or f"no output (exit code {result.returncode})"
    except FileNotFoundError:
        return f"NOT FOUND at {path}"
    except Exception as e:
        return f"error checking version: {e}"


@app.get("/")
def health_check():
    # Reporting these here means you can sanity-check what's actually
    # installed just by hitting the deployed URL -- no shell access needed.
    return {
        "status": "ok",
        "message": "Naukri Selenium Docker Service is running!",
        "chrome_bin": CHROME_BIN,
        "chrome_version": _binary_version(CHROME_BIN),
        "chromedriver_path": CHROMEDRIVER_PATH,
        "chromedriver_version": _binary_version(CHROMEDRIVER_PATH),
        "scrape_do_token_set": bool(os.getenv("SCRAPE_DO_TOKEN", "")),
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
        # logger.exception captures the full traceback in Render's logs, not
        # just str(e) -- which is often empty for native Selenium crashes.
        logger.exception("Scraping failed for url=%s", url)
        raise HTTPException(
            status_code=500, detail=f"Scraping failed: {str(e)}"
        )
