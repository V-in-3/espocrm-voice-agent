import re
from typing import Optional

from playwright.sync_api import Page, sync_playwright

DEMO_URL = "https://demo.us.espocrm.com/"


class CrmBrowser:
    def __init__(self, headless: bool = True):
        self._pw = sync_playwright().start()
        # System Chrome: bundled Chromium download is blocked by the corporate proxy.
        self._browser = self._pw.chromium.launch(channel="chrome", headless=headless)
        self.page: Page = self._browser.new_page(viewport={"width": 1280, "height": 720})

    def close(self) -> None:
        self._browser.close()
        self._pw.stop()

    def login(self) -> None:
        self.page.goto(DEMO_URL, wait_until="networkidle")
        self.page.get_by_role("button", name="Login").click()
        self.page.wait_for_selector("#nav-menu-toggle, .navbar", timeout=15000)

    def read_screen(self) -> str:
        return self.page.locator("body").aria_snapshot()

    def open_contact_form(self) -> None:
        self.page.goto(DEMO_URL + "#Contact/create", wait_until="networkidle")
        self.page.wait_for_selector('input[data-name="lastName"]')

    def fill_field(self, field: str, value: str) -> None:
        selectors = {
            "firstName": 'input[data-name="firstName"]',
            "lastName": 'input[data-name="lastName"]',
            "email": 'div[data-name="emailAddress"] input[type="email"], div[data-name="emailAddress"] input.email-address',
            "phone": 'div[data-name="phoneNumber"] input.phone-number',
        }
        loc = self.page.locator(selectors[field]).first
        loc.fill(value)

    def read_form(self) -> dict:
        def val(sel: str) -> str:
            return self.page.locator(sel).first.input_value()

        return {
            "firstName": val('input[data-name="firstName"]'),
            "lastName": val('input[data-name="lastName"]'),
            "email": val('div[data-name="emailAddress"] input[type="email"], div[data-name="emailAddress"] input.email-address'),
        }

    def save(self) -> Optional[str]:
        self.page.get_by_role("button", name="Save", exact=True).click()
        try:
            self.page.wait_for_url(re.compile(r"#Contact/view/"), timeout=10000)
        except Exception:
            return None
        return self.page.url.rsplit("/", 1)[-1]

    def verify_saved_contact(self, record_id: str, first: str, last: str, email: str = "") -> dict:
        self.page.goto(DEMO_URL + f"#Contact/view/{record_id}", wait_until="networkidle")
        self.page.wait_for_selector(".record, .detail", timeout=10000)
        expected = {"name": f"{first} {last}".strip()}
        if email:
            expected["email"] = email

        # Fields render asynchronously; poll instead of trusting a single early read.
        checks = {key: False for key in expected}
        for _ in range(20):
            text = self.page.locator("body").inner_text()
            checks = {key: value in text for key, value in expected.items()}
            if all(checks.values()):
                break
            self.page.wait_for_timeout(500)
        return checks
