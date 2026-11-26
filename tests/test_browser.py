import unittest
from unittest.mock import Mock

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from browser import CrmBrowser


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.crm = CrmBrowser.__new__(CrmBrowser)
        self.crm.page = Mock()
        self.page = self.crm.page
        self.page.is_closed.return_value = False
        self.page.locator.return_value.all_inner_texts.return_value = []
        self.page.get_by_role.return_value.first.is_visible.return_value = False
        self.page.url = "https://demo.us.espocrm.com/#Contact/view/record-1"

    def test_save_returns_record_id_and_removes_response_listener(self):
        self.assertEqual(self.crm.save(), "record-1")
        self.page.get_by_role.return_value.click.assert_called_once()
        self.page.remove_listener.assert_called_once_with("response", self.page.on.call_args.args[1])

    def test_save_timeout_reports_crm_response_and_alert_without_retrying(self):
        def click():
            response = Mock()
            response.request.method = "POST"
            response.url = "https://demo.us.espocrm.com/api/v1/Contact"
            response.status = 400
            self.page.on.call_args.args[1](response)

        self.page.get_by_role.return_value.click.side_effect = click
        self.page.wait_for_url.side_effect = PlaywrightTimeoutError("timeout")
        self.page.locator.return_value.all_inner_texts.return_value = ["Bad request: invalid value"]

        with self.assertRaises(RuntimeError) as error:
            self.crm.save()

        self.assertIn("HTTP 400", str(error.exception))
        self.assertIn("invalid value", str(error.exception))
        self.assertIn("outcome is unknown", str(error.exception))
        self.assertIn("before trying again", str(error.exception))
        self.page.get_by_role.return_value.click.assert_called_once()
        self.page.remove_listener.assert_called_once()

    def test_timeout_without_response_still_warns_about_unknown_outcome(self):
        self.page.wait_for_url.side_effect = PlaywrightTimeoutError("timeout")
        with self.assertRaisesRegex(RuntimeError, "outcome is unknown"):
            self.crm.save()
        self.page.remove_listener.assert_called_once()


if __name__ == "__main__":
    unittest.main()
