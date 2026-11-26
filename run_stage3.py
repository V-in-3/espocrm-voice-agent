import os
import re
import sys

from agent import run_agent
from browser import CrmBrowser
from llm_client import build_client
from voice import listen, speak

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CONFIDENCE_THRESHOLD = 0.6
INTERRUPT_CONFIDENCE_THRESHOLD = 0.3  # looser - just enough to tell "something was said" from noise


def narrate(tool_name: str, args: dict) -> None:
    """Speaks what the agent is about to do, so a listener can follow along as it
    navigates the CRM - not just a silent action with a result printed to a log."""
    if tool_name == "fill_field":
        speak(f"Setting {args['field']} to {args['value']}")
    elif tool_name == "save":
        speak("Saving the contact now")


def make_pre_save_hook(seconds: float = 4.0):
    """Opens a short window right before Save for a spoken correction. Silence or
    pure noise (low transcription confidence) is treated as 'nothing to change' and
    the save proceeds; a real correction is handed back to run_agent instead of
    letting the save go through unexamined."""

    def pre_save_hook():
        speak("About to save. Say a correction now, or stay quiet to continue.")
        text, confidence = listen(seconds, cue=False)
        if text and confidence >= INTERRUPT_CONFIDENCE_THRESHOLD:
            print(f"[interrupt] heard correction: {text!r} (confidence={confidence:.2f})")
            speak("Got it, updating before I save.")
            return text
        return None

    return pre_save_hook


def make_clarify_hook(seconds: float = 6.0):
    """CLI counterpart of the web app's clarify_hook: speaks the model's question
    and listens for a reply, so a request with missing/unclear info turns into a
    real back-and-forth instead of ending the whole run on the first question."""

    def clarify_hook(question: str) -> str:
        speak(question)
        text, confidence = listen(seconds)
        print(f"[clarify] heard: {text!r} (confidence={confidence:.2f})")
        return text or "(no response heard, please proceed with your best interpretation or ask again)"

    return clarify_hook


def make_confirm_field(low_confidence: bool):
    """Interactive fallback for values Whisper is likely to have gotten wrong.
    Runs only in stage 3 - stage 2's mock/scripted runs never call this.

    low_confidence forces a review of the email too, even if it happens to match
    EMAIL_RE - adaptive routing by confidence, using Whisper's own avg_logprob
    instead of a fixed regex-only check."""

    def confirm_field(field: str, value: str) -> str:
        try:
            if field == "email" and (low_confidence or not EMAIL_RE.match(value)):
                reason = "low-confidence transcription" if low_confidence else "doesn't look like a valid address"
                print(f"[voice] heard email '{value}', {reason}.")
                typed = input("Type the correct email (Enter to keep it as heard): ").strip()
                return typed or value
            if field in ("firstName", "lastName"):
                typed = input(f"Heard {field} = '{value}'. Enter to accept, or type a correction: ").strip()
                return typed or value
        except EOFError:
            # No interactive terminal attached - fall back to whatever was heard.
            pass
        return value

    return confirm_field


def main() -> int:
    backend = os.environ.get("LLM_BACKEND", "ollama")
    if backend == "mock":
        print("FAIL: mock backend can't interpret a real voice transcript, use ollama or groq")
        return 1
    client = build_client(backend)

    seconds = float(os.environ.get("VOICE_SECONDS", "6"))
    user_text, confidence = listen(seconds)
    low_confidence = confidence < CONFIDENCE_THRESHOLD
    print(f"[recognized] {user_text!r} (confidence={confidence:.2f}, low={low_confidence})")
    if not user_text:
        print("FAIL: got no transcript, nothing was recognized")
        return 1

    crm = CrmBrowser(headless="--headed" not in sys.argv)
    try:
        crm.login()
        crm.open_contact_form()

        state = run_agent(
            client,
            crm,
            user_text,
            confirm_fn=make_confirm_field(low_confidence),
            narrate_fn=narrate,
            pre_save_hook=make_pre_save_hook(),
            clarify_hook=make_clarify_hook(),
        )
        print("model:", state["final_message"])

        if not state["record_id"]:
            print("FAIL: agent never called save, or save returned no record id")
            speak("Something went wrong, the contact was not saved.")
            return 1

        filled = state["filled"]
        # Never trust the model's own claim of success: reopen the record and read
        # back what actually landed, same discipline as stages 1 and 2. The voice
        # confirmation only fires after this check passes, not before.
        checks = crm.verify_saved_contact(state["record_id"], filled["firstName"], filled["lastName"], filled["email"])
        print("record id:", state["record_id"])
        print("checks:", checks)
        ok = all(checks.values())
        print("RESULT:", "OK" if ok else "FAIL")

        if ok:
            speak(f"Saved a contact for {filled['firstName']} {filled['lastName']}")
        else:
            speak("The contact was saved, but verification failed.")
        return 0 if ok else 1
    finally:
        crm.close()


if __name__ == "__main__":
    sys.exit(main())
