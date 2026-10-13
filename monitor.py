
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

LOGIN_URL = "https://myinl.inll.lu/fr_FR/web/login"
REGISTRATION_URL = os.getenv(
    "MYINL_REGISTRATION_URL",
    "https://myinl.inll.lu/my/home"
)

STATE_FILE = Path("state.json")
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
EMAIL = os.environ["MYINL_EMAIL"]
PASSWORD = os.environ["MYINL_PASSWORD"]

EXAM_KEYWORDS = ("sproochentest",)
EXAM_DATE = os.getenv("EXAM_DATE", "30/09/2026")


def send_telegram(message):
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    response.raise_for_status()

    if not response.json().get("ok"):
        raise RuntimeError("Telegram rejected the notification")


def load_state():
    if not STATE_FILE.exists():
        return None

    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def normalize(text):
    return re.sub(r"\s+", " ", text).strip()


def login(page):
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)

    page.get_by_label("E-mail").fill(EMAIL)
    page.get_by_label("Mot de passe").fill(PASSWORD)
    page.get_by_role("button", name="Se connecter").click()

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(2000)

    current_url = page.url.lower()
    page_text = normalize(page.locator("body").inner_text())

    if "web/login" in current_url or "mot de passe" in page_text.lower():
        # A login page can also contain a password-reset link, so inspect
        # the actual password input before declaring authentication failed.
        if page.locator('input[type="password"]').count() > 0:
            raise RuntimeError(
                "Login may have failed. Check credentials or any additional "
                "authentication challenge."
            )


def open_registrations(page):
    # If your account uses a different route, set MYINL_REGISTRATION_URL
    # in the GitHub Actions workflow.
    page.goto(
        REGISTRATION_URL,
        wait_until="domcontentloaded",
        timeout=45000,
    )

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    if "web/login" in page.url.lower():
        raise RuntimeError("MyINL redirected to login; authentication expired")

    body = normalize(page.locator("body").inner_text())

    if "Mes inscriptions".lower() not in body.lower():
        # Try the visible account navigation if the dashboard route differs.
        link = page.get_by_text("Mes inscriptions", exact=False)

        if link.count() > 0:
            link.first.click()
            page.wait_for_timeout(2000)
            body = normalize(page.locator("body").inner_text())

    return body


def find_exam_registration(page):
    # Search rendered table rows rather than the whole page, so unrelated
    # page changes do not trigger a false result alert.
    rows = page.locator("tr")
    matches = []

    for index in range(rows.count()):
        row = rows.nth(index)

        if not row.is_visible():
            continue

        row_text = normalize(row.inner_text())

        if not all(keyword in row_text.lower() for keyword in EXAM_KEYWORDS):
            continue

        # Match the exam date when it is present in the row.
        if EXAM_DATE not in row_text and "2026-09-30" not in row_text:
            continue

        documents = []

        links = row.locator("a")
        for link_index in range(links.count()):
            item = links.nth(link_index)
            href = item.get_attribute("href")

            if href:
                documents.append(urljoin(page.url, href))

        # Include the row text and document links in the fingerprint.
        matches.append({
            "text": row_text,
            "documents": sorted(set(documents)),
        })

    if not matches:
        raise RuntimeError(
            "Could not find the expected Sproochentest registration row. "
            "The page layout, URL, or date may need adjustment. "
            "No state was updated."
        )

    if len(matches) > 1:
        raise RuntimeError(
            "Found multiple matching exam rows. Refine the exam selector "
            "before monitoring to avoid tracking the wrong registration."
        )

    return matches[0]


def main():
    previous = load_state()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        context = browser.new_context(
            locale="fr-FR",
            accept_downloads=False,
        )

        page = context.new_page()
        page.set_default_timeout(15000)

        try:
            login(page)
            open_registrations(page)
            registration = find_exam_registration(page)

            new_state = {
                "registration": registration,
            }

            if previous is None:
                save_state(new_state)
                send_telegram(
                    "INL monitor started.\n"
                    "Your Sproochentest registration was found.\n"
                    "I will notify you when its displayed information changes."
                )
                print("Initial state saved; no change alert sent.")
                return

            if previous.get("registration") != registration:
                send_telegram(
                    "INL Sproochentest registration changed!\n\n"
                    f"Previous: {previous.get('registration', {}).get('text', 'Unknown')}\n\n"
                    f"Current: {registration['text']}\n\n"
                    "Log in to MyINL to verify whether your results have "
                    "been published:\n"
                    "https://myinl.inll.lu/fr_FR/web/login"
                )

                save_state(new_state)
                print("Registration change detected and notification sent.")
            else:
                print("No change detected.")

        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Avoid printing credentials, tokens, or page contents to logs.
        print(f"Monitor failed: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
