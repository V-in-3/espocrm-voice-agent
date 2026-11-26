import os
import sys
import time

from agent import run_agent
from browser import CrmBrowser
from llm_client import LLMResponse, MockClient, ToolCall, build_client


def mock_script(first: str, last: str, email: str) -> list:
    # Scripted stand-in for a real model: same tool-call shape the loop expects
    # from Groq/Ollama, so the orchestration can be proven without any network call.
    return [
        LLMResponse(content=None, tool_calls=[ToolCall("c1", "fill_field", {"field": "firstName", "value": first})]),
        LLMResponse(content=None, tool_calls=[ToolCall("c2", "fill_field", {"field": "lastName", "value": last})]),
        LLMResponse(content=None, tool_calls=[ToolCall("c3", "fill_field", {"field": "email", "value": email})]),
        LLMResponse(content=None, tool_calls=[ToolCall("c4", "save", {})]),
        LLMResponse(content="Saved the contact.", tool_calls=[]),
    ]


def main() -> int:
    stamp = int(time.time())
    first, last = "Ivan", f"Petrenko{stamp % 10000}"
    email = f"ivan{stamp % 10000}@example.com"

    backend = os.environ.get("LLM_BACKEND", "mock")
    client = MockClient(mock_script(first, last, email)) if backend == "mock" else build_client(backend)

    crm = CrmBrowser(headless="--headed" not in sys.argv)
    try:
        crm.login()
        crm.open_contact_form()

        state = run_agent(client, crm, f"Create a contact: first name {first}, last name {last}, email {email}.")
        print("model:", state["final_message"])

        if not state["record_id"]:
            print("FAIL: agent never called save, or save returned no record id")
            return 1

        filled = state["filled"]
        # Same discipline as stage 1: never trust the model's own claim of success,
        # reopen the record and read it back.
        checks = crm.verify_saved_contact(state["record_id"], filled["firstName"], filled["lastName"], filled["email"])
        print("record id:", state["record_id"])
        print("checks:", checks)
        ok = all(checks.values())
        print("RESULT:", "OK" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        crm.close()


if __name__ == "__main__":
    sys.exit(main())
