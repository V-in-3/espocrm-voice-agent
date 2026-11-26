import unittest

from fastapi.testclient import TestClient

from agent import run_agent
from llm_client import LLMResponse, MockClient, ToolCall
from server import _is_confirmation, app, make_web_pre_save_hook, SESSIONS, SessionState


def call(name, arguments=None, call_id="c"):
    return LLMResponse(None, [ToolCall(call_id, name, arguments or {})])


class FakeCrm:
    def __init__(self):
        self.form = {"firstName": "", "lastName": "", "email": "", "accounts": []}
        self.saves = 0
        self.lookup = {}

    def read_screen(self):
        return "contact form"

    def fill_field(self, field, value):
        self.form[field] = value

    def read_form(self):
        return dict(self.form)

    def save(self):
        self.saves += 1
        return "record-1"

    def link_account(self, name, confirmed=False):
        if self.lookup.get(name) == "ambiguous" and not confirmed:
            return False, f"Multiple accounts found. Specify the exact account name: {name}."
        if self.lookup.get(name):
            self.form["accounts"] = [name]
            return True, f"Linked account {name}."
        return False, f"No unique match for {name}."

    def clear_accounts(self):
        self.form["accounts"] = []


class AgentGraphTests(unittest.TestCase):
    def setUp(self):
        self.crm = FakeCrm()

    def fill_required(self):
        return [
            call("fill_field", {"field": "firstName", "value": "Jane"}),
            call("fill_field", {"field": "lastName", "value": "Example"}),
            call("fill_field", {"field": "email", "value": "jane@example.com"}),
        ]

    def test_graph_fills_optional_fields_and_saves_after_confirmation(self):
        script = self.fill_required() + [
            call("fill_field", {"field": "phone", "value": "+123456"}),
            call("fill_field", {"field": "addressCity", "value": "Boston"}),
            call("fill_field", {"field": "addressCountry", "value": "United States"}),
            call("save"),
        ]
        confirmations = []
        result = run_agent(MockClient(script), self.crm, "create contact",
                           pre_save_hook=lambda: confirmations.append(True))
        self.assertEqual(self.crm.saves, 1)
        self.assertEqual(confirmations, [True])
        self.assertEqual(result["filled"]["addressCity"], "Boston")
        self.assertEqual(result["filled"]["addressCountry"], "United States")
        self.assertEqual(result["record_id"], "record-1")

    def test_missing_and_invalid_email_block_save_before_confirmation(self):
        script = [
            call("save"),
            call("fill_field", {"field": "firstName", "value": "Jane"}),
            call("fill_field", {"field": "lastName", "value": "Example"}),
            call("fill_field", {"field": "email", "value": "invalid"}),
            call("save"),
            call("fill_field", {"field": "email", "value": "jane@example.com"}),
            call("save"),
        ]
        confirmations = []
        run_agent(MockClient(script), self.crm, "create contact",
                  pre_save_hook=lambda: confirmations.append(True))
        self.assertEqual(self.crm.saves, 1)
        self.assertEqual(confirmations, [True])

    def test_correction_is_applied_and_confirmed_before_saving(self):
        script = self.fill_required() + [
            call("save"),
            call("fill_field", {"field": "lastName", "value": "Updated"}),
            call("save"),
        ]
        replies = iter(["change last name to Updated", None])
        result = run_agent(MockClient(script), self.crm, "create contact",
                           pre_save_hook=lambda: next(replies))
        self.assertEqual(result["filled"]["lastName"], "Updated")
        self.assertEqual(self.crm.saves, 1)

    def test_unresolved_account_blocks_save_until_explicit_skip(self):
        self.crm.form["accounts"] = ["Previous"]
        script = self.fill_required() + [
            call("link_account", {"name": "Unknown"}),
            call("save"),
            call("save"),
        ]
        replies = iter(["try again", "skip account"])
        questions = []

        def clarify(question):
            questions.append(question)
            return next(replies)

        result = run_agent(MockClient(script), self.crm, "create contact", clarify_hook=clarify)
        self.assertEqual(len(questions), 2)
        self.assertEqual(result["filled"]["accounts"], [])
        self.assertEqual(self.crm.saves, 1)

    def test_unresolved_account_can_be_replaced_with_exact_match(self):
        self.crm.lookup["Example Co"] = True
        script = self.fill_required() + [
            call("link_account", {"name": "Missing"}),
            call("link_account", {"name": "Example Co"}),
            call("save"),
        ]
        result = run_agent(MockClient(script), self.crm, "create contact",
                           clarify_hook=lambda _question: "Example Co")
        self.assertEqual(result["filled"]["accounts"], ["Example Co"])
        self.assertEqual(self.crm.saves, 1)

    def test_account_cannot_be_cleared_by_model_while_unresolved(self):
        script = self.fill_required() + [
            call("link_account", {"name": "Missing"}),
            call("clear_account"),
            call("save"),
        ]
        replies = iter(["try again", "skip account"])
        run_agent(MockClient(script), self.crm, "create contact",
                  clarify_hook=lambda _question: next(replies))
        self.assertEqual(self.crm.saves, 1)

    def test_similar_accounts_require_user_confirmation(self):
        self.crm.lookup["Example Co"] = "ambiguous"
        script = self.fill_required() + [
            call("link_account", {"name": "Example Co"}),
            call("link_account", {"name": "Example Co"}),
            call("save"),
        ]
        questions = []

        def clarify(question):
            questions.append(question)
            return "Example Co"

        result = run_agent(MockClient(script), self.crm, "create contact", clarify_hook=clarify)
        self.assertEqual(len(questions), 1)
        self.assertEqual(result["filled"]["accounts"], ["Example Co"])

    def test_web_confirmation_requires_an_explicit_positive_response(self):
        for reply in (
            "yes", "yes, yes", "confirm to continue", "Confirm to continue!", "everything is good", "please save it",
            "Save contact, please.", "Please save contact, that's all.",
        ):
            self.assertTrue(_is_confirmation(reply), reply)
        for reply in (
            "", "no", "don't save", "not yet", "everything is wrong",
            "don't save contact, please", "save contact but change the email",
            "confirm to continue but change the email",
        ):
            self.assertFalse(_is_confirmation(reply), reply)
        session = SessionState()
        session.correction_queue.put(None)
        session.correction_queue.put("yes")
        self.assertIsNone(make_web_pre_save_hook(session)())
        self.assertTrue(any("did not hear" in line for line in session.log))

    def test_confirm_to_continue_saves_the_contact(self):
        session = SessionState()
        session.correction_queue.put("confirm to continue")
        result = run_agent(
            MockClient(self.fill_required() + [call("save")]),
            self.crm,
            "create contact",
            pre_save_hook=make_web_pre_save_hook(session),
        )
        self.assertEqual(self.crm.saves, 1)
        self.assertEqual(result["record_id"], "record-1")

    def test_confirm_button_only_accepts_a_pending_save(self):
        session_id = "test-confirm-button"
        session = SessionState(status="awaiting_correction")
        SESSIONS[session_id] = session
        self.addCleanup(SESSIONS.pop, session_id, None)
        client = TestClient(app)
        url = f"/api/session/{session_id}/confirm"

        self.assertEqual(client.post(url).status_code, 409)
        self.assertTrue(session.correction_queue.empty())
        session.status = "awaiting_confirmation"
        self.assertEqual(client.post(url).status_code, 200)
        self.assertEqual(session.correction_queue.get_nowait(), "yes")
        self.assertEqual(client.post(url).status_code, 409)
        self.assertEqual(client.post("/api/session/unknown/confirm").status_code, 404)

    def test_correction_prevents_a_second_stale_confirmation(self):
        session_id = "test-correction-race"
        session = SessionState(status="awaiting_confirmation")
        SESSIONS[session_id] = session
        self.addCleanup(SESSIONS.pop, session_id, None)
        client = TestClient(app)
        base = f"/api/session/{session_id}"

        self.assertEqual(client.post(base + "/correction", data={"text": "change last name"}).status_code, 200)
        self.assertEqual(client.post(base + "/confirm").status_code, 409)
        self.assertEqual(session.correction_queue.get_nowait(), "change last name")
        self.assertTrue(session.correction_queue.empty())


if __name__ == "__main__":
    unittest.main()
