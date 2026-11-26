import re
from typing import Optional
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Response, TimeoutError as PlaywrightTimeoutError, sync_playwright

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
            "addressStreet": 'textarea[data-name="addressStreet"]',
            "addressCity": 'input[data-name="addressCity"]',
            "addressState": 'input[data-name="addressState"]',
            "addressPostalCode": 'input[data-name="addressPostalCode"]',
            "addressCountry": 'input[data-name="addressCountry"]',
        }
        loc = self.page.locator(selectors[field]).first
        loc.fill(value)

    def read_form(self) -> dict:
        def val(sel: str) -> str:
            return self.page.locator(sel).first.input_value()

        result: dict[str, str | list[str]] = {
            "firstName": val('input[data-name="firstName"]'),
            "lastName": val('input[data-name="lastName"]'),
            "email": val('div[data-name="emailAddress"] input[type="email"], div[data-name="emailAddress"] input.email-address'),
            "phone": val('div[data-name="phoneNumber"] input.phone-number'),
            "addressStreet": val('textarea[data-name="addressStreet"]'),
            "addressCity": val('input[data-name="addressCity"]'),
            "addressState": val('input[data-name="addressState"]'),
            "addressPostalCode": val('input[data-name="addressPostalCode"]'),
            "addressCountry": val('input[data-name="addressCountry"]'),
        }
        result["accounts"] = self.page.locator('.field[data-name="accounts"] .link-item-name').all_inner_texts()
        return result

    def link_account(self, name: str, confirmed: bool = False) -> tuple[bool, str]:
        name = name.strip()
        if not name:
            return False, "Account name is empty."

        field = self.page.locator('.field[data-name="accounts"]')
        selected_names = field.locator(".link-item-name").all_inner_texts()
        if len(selected_names) == 1 and selected_names[0].strip().casefold() == name.casefold():
            return True, f"Account {name} is already linked."
        previous_ids = field.locator(".link-group-item-with-columns").evaluate_all(
            "(items) => items.map(item => item.dataset.id)"
        )
        field.locator('button[data-action="selectLink"]').click()
        dialog = self.page.get_by_role("dialog")
        dialog.wait_for()
        dialog.locator("tbody").wait_for()
        dialog.locator('input[data-name="textFilter"]').fill(name)
        with self.page.expect_response(
            lambda response: "/api/v1/Account?" in response.url and response.request.method == "GET"
        ) as search:
            dialog.locator('button[data-action="search"]').click()
        if not search.value.ok:
            raise RuntimeError(f"Account search failed: HTTP {search.value.status}")
        result_ids = [item["id"] for item in search.value.json()["list"]]
        self.page.wait_for_function(
            """ids => {
                const rows = [...document.querySelectorAll('[role="dialog"] tbody tr[data-id]')];
                return rows.length === ids.length && rows.every(row => ids.includes(row.dataset.id));
            }""",
            arg=result_ids,
        )
        rows = dialog.locator("tbody tr")
        show_more = dialog.get_by_role("button", name=re.compile("Show more"))
        while show_more.is_visible():
            with self.page.expect_response(
                lambda response: "/api/v1/Account?" in response.url and response.request.method == "GET"
            ) as more:
                show_more.click()
            if not more.value.ok:
                raise RuntimeError(f"Account search failed: HTTP {more.value.status}")
            result_ids.extend(item["id"] for item in more.value.json()["list"])
            self.page.wait_for_function(
                """ids => {
                    const rows = [...document.querySelectorAll('[role="dialog"] tbody tr[data-id]')];
                    return rows.length === ids.length && rows.every(row => ids.includes(row.dataset.id));
                }""",
                arg=result_ids,
            )

        matches = []
        names = []
        for i in range(rows.count()):
            row = rows.nth(i)
            link = row.locator('td[data-name="name"] a')
            if link.count():
                candidate = link.inner_text().strip()
                names.append(candidate)
                if candidate.casefold() == name.casefold():
                    matches.append(row)
        if len(names) > 1 and not confirmed:
            dialog.get_by_role("button", name="Cancel", exact=True).click()
            options = ", ".join(names[:10])
            return False, f"Multiple accounts found: {options}. Please reply with the exact account name or say skip account."
        if len(matches) != 1:
            dialog.get_by_role("button", name="Cancel", exact=True).click()
            reason = "No exact account match" if not matches else "Multiple accounts have that exact name"
            return False, f"{reason}: {name}. Ask the user for another account name or whether to skip the account."

        matches[0].locator('input[type="checkbox"]').check()
        dialog.get_by_role("button", name="Select", exact=True).click()
        for account_id in previous_ids:
            field.locator(f'[data-action="clearLink"][data-id="{account_id}"]').click()
        return True, f"Linked account {name}."

    def clear_accounts(self) -> None:
        links = self.page.locator('.field[data-name="accounts"] [data-action="clearLink"]')
        while links.count():
            links.first.click()

    def save(self) -> Optional[str]:
        save_status: Optional[int] = None

        def capture_response(response: Response) -> None:
            nonlocal save_status
            if (response.request.method == "POST"
                    and urlsplit(response.url).path.rstrip("/") == "/api/v1/Contact"):
                save_status = response.status

        self.page.on("response", capture_response)
        try:
            self.page.get_by_role("button", name="Save", exact=True).click()
            self.page.wait_for_url(re.compile(r"#Contact/view/"), timeout=10000)
            return self.page.url.rsplit("/", 1)[-1]
        except PlaywrightTimeoutError as exc:
            details = []
            if save_status is not None:
                details.append(f"CRM save response: HTTP {save_status}.")
            try:
                if not self.page.is_closed():
                    alerts = self.page.locator('[role="alert"]:visible, .alert:visible').all_inner_texts()
                    if alerts:
                        details.append("CRM message: " + " ".join(alerts)[:500])
                    if self.page.get_by_role("dialog").first.is_visible():
                        details.append("CRM displayed a dialog.")
            except PlaywrightError:
                details.append("CRM screen details could not be read.")
            raise RuntimeError(
                "Could not confirm that the contact was saved. "
                + " ".join(details)
                + " The save outcome is unknown; check the CRM contacts list before trying again to avoid duplicates."
            ) from exc
        finally:
            self.page.remove_listener("response", capture_response)

    def verify_saved_contact(
        self, record_id: str, first: str, last: str, email: str = "", filled: Optional[dict] = None
    ) -> dict:
        self.page.goto(DEMO_URL + f"#Contact/view/{record_id}", wait_until="networkidle")
        self.page.wait_for_selector(".record, .detail", timeout=10000)
        expected = {"name": f"{first} {last}".strip()}
        if email:
            expected["email"] = email
        if filled:
            for field in ("phone", "addressStreet", "addressCity", "addressState", "addressPostalCode", "addressCountry"):
                if filled.get(field):
                    expected[field] = filled[field]
            for index, name in enumerate(filled.get("accounts", [])):
                expected[f"account{index + 1}"] = name.strip()

        # Fields render asynchronously; poll instead of trusting a single early read.
        checks = {key: False for key in expected}
        for _ in range(20):
            text = " ".join(self.page.locator("body").inner_text().split())
            checks = {key: " ".join(value.split()) in text for key, value in expected.items()}
            if all(checks.values()):
                break
            self.page.wait_for_timeout(500)
        return checks
