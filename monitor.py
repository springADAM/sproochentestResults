
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

LOGIN_URL = "https://myinl.inll.lu/web/login"
REGISTRATION_URL = os.getenv(
    "MYINL_REGISTRATION_URL",
    "https://myinl.inll.lu/my/exam/registrations",
)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
EMAIL = os.environ["MYINL_EMAIL"]
PASSWORD = os.environ["MYINL_PASSWORD"]

EXAM_KEYWORD = "sproochentest"
EXAM_DATE = os.getenv("EXAM_DATE", "30/09/2026")
STATE_FILE = Path("state.json")


def normalize(value):
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", value).strip().lower()


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
        raise RuntimeError("Telegram failed to send the notification")


def load_state():
    if not STATE_FILE.exists():
        return None

    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "state.json is unreadable. Refusing to reset the baseline."
        ) from error


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def login(page):
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)

    # If the browser is already authenticated, the login page may redirect.
    if "/web/login" not in page.url.lower():
        return

    email_input = page.locator(
        'input[type="email"], input[name="login"], input[name="email"]'
    ).first
    password_input = page.locator('input[type="password"]').first

    if email_input.count() == 0 or password_input.count() == 0:
        raise RuntimeError(
            "Could not identify the MyINL login fields. "
            "The login page may have changed."
        )

    email_input.fill(EMAIL)
    password_input.fill(PASSWORD)

    submit = page.locator(
        'button[type="submit"], input[type="submit"]'
    ).first

    if submit.count() == 0:
        raise RuntimeError("Could not identify the login submit button")

    submit.click()

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(1500)

    if "/web/login" in page.url.lower():
        body = normalize(page.locator("body").inner_text())

        if (
            "invalid" in body
            or "incorrect" in body
            or "invalide" in body
            or "incorrect" in body
        ):
            raise RuntimeError(
                "MyINL rejected the login. Check your credentials."
            )

        raise RuntimeError(
            "MyINL still shows the login page. Authentication may require "
            "different form selectors, additional verification, or a CAPTCHA."
        )


def get_registration_row(page):
    page.goto(
        REGISTRATION_URL,
        wait_until="domcontentloaded",
        timeout=45000,
    )

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    if "/web/login" in page.url.lower():
        raise RuntimeError(
            "MyINL redirected to login. Authentication did not persist."
        )

    page_heading = normalize(page.locator("body").inner_text())

    if "mes inscriptions" not in page_heading:
        raise RuntimeError(
            "The expected Mes inscriptions page did not load. "
            "Check MYINL_REGISTRATION_URL."
        )

    tables = page.locator("table")
    matching_rows = []

    for table_index in range(tables.count()):
        table = tables.nth(table_index)
        headers = table.locator("thead tr").first.locator("th, td")

        if headers.count() == 0:
            headers = table.locator("tr").first.locator("th, td")

        header_names = [
            normalize(headers.nth(i).inner_text())
            for i in range(headers.count())
        ]

        publication_index = next(
            (
                i for i, name in enumerate(header_names)
                if "date de publication des resultats" in name
            ),
            None,
        )

        if publication_index is None:
            continue

        rows = table.locator("tbody tr")

        for row_index in range(rows.count()):
            row = rows.nth(row_index)

            if not row.is_visible():
                continue

            cells = row.locator("td")
            if cells.count() <= publication_index:
                continue

            row_text = normalize(row.inner_text())

            if EXAM_KEYWORD not in row_text:
                continue

            normalized_date = normalize(EXAM_DATE)
            iso_date = "2026-09-30"

            if normalized_date not in row_text and iso_date not in row_text:
                continue

            publication_date = cells.nth(publication_index).inner_text().strip()

            status_index = next(
                (
                    i for i, name in enumerate(header_names)
                    if name == "statut"
                ),
                None,
            )

            status = (
                cells.nth(status_index).inner_text().strip()
                if status_index is not None and cells.count() > status_index
                else ""
            )

            matching_rows.append({
                "publication_date": publication_date,
                "status": status,
            })

    if len(matching_rows) == 0:
        raise RuntimeError(
            "Could not find the expected Sproochentest row or publication "
            "date column. No state was changed."
        )

    if len(matching_rows) > 1:
        raise RuntimeError(
            "Multiple matching Sproochentest registrations were found. "
            "Refusing to monitor an ambiguous registration."
        )

    return matching_rows[0]


def main():
    previous_state = load_state()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(locale="fr-FR")
        page = context.new_page()
        page.set_default_timeout(15000)

        try:
            login(page)
            registration = get_registration_row(page)

            current_date = registration["publication_date"]
            previous_registration = (
                previous_state.get("registration")
                if previous_state
                else None
            )

            current_state = {"registration": registration}

            if previous_state is None:
                save_state(current_state)

                if current_date:
                    send_telegram(
                        "INL Sproochentest results may already be available.\n"
                        f"Publication date shown: {current_date}\n"
                        f"Registration status: {registration['status'] or 'Not shown'}\n\n"
                        f"Verify your account: {REGISTRATION_URL}"
                    )
                    print("Initial check found a publication date.")
                else:
                    send_telegram(
                        "INL results monitor is active.\n"
                        "Your Sproochentest registration was found, and its "
                        "results publication date is currently empty.\n"
                        "I will notify you when a date appears."
                    )
                    print("Initial baseline saved; no results date shown.")

                return

            old_date = (
                previous_registration.get("publication_date", "")
                if previous_registration
                else ""
            )

            if current_date and current_date != old_date:
                send_telegram(
                    "INL Sproochentest results publication date detected!\n\n"
                    f"Publication date: {current_date}\n"
                    f"Registration status: {registration['status'] or 'Not shown'}\n\n"
                    "Log in to verify your results:\n"
                    f"{REGISTRATION_URL}"
                )
                save_state(current_state)
                print("Publication date changed; Telegram alert sent.")

            elif not current_date and old_date:
                save_state(current_state)
                print("Publication date is empty again; state updated.")

            else:
                # Save status changes without claiming they mean results
                # have been published.
                if previous_registration != registration:
                    save_state(current_state)

                send_telegram("INL Sproochentest monitor check completed.\n\n"
                f"Publication date: {current_date or 'Not published yet'}\n"
                f"Registration status: {registration['status'] or 'Not shown'}\n\n"
                "I will check again at the next scheduled run."
                )
                print("No new results publication date detected. Status message sent.")

        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never print tokens, passwords, cookies, or page contents.
        print(
            f"Monitor failed: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        sys.exit(1)
