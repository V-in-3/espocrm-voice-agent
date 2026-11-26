import os
import queue
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from typing import Optional

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from agent import run_agent
from browser import CrmBrowser
from llm_client import build_client
from voice import spoken_to_dotted, transcribe

app = FastAPI()


@dataclass
class SessionState:
    status: str = "running"  # running | awaiting_correction | done | error
    log: list = field(default_factory=list)
    correction_queue: "queue.Queue" = field(default_factory=queue.Queue)
    result: Optional[dict] = None


SESSIONS: dict = {}


def make_web_narrate(session: SessionState):
    def narrate(tool_name: str, args: dict) -> None:
        if tool_name == "fill_field":
            session.log.append(f"Setting {args['field']} to {args['value']}")
        elif tool_name == "save":
            session.log.append("Saving the contact now")

    return narrate


# There are endless ways to say "go ahead" (yes / continue / everything is good /
# please save it / yes, yes) but a real correction almost always names a field or
# uses a handful of signal words - so default to "this is a confirmation" and only
# treat it as a correction when one of those signals is actually present. That is
# the safer direction to be wrong in: a stray "correction" with no real content
# just makes the model ask which field to change, instead of looping on a pause
# forever because "please just save it" wasn't in a fixed phrase list.
_CORRECTION_SIGNALS = (
    "change",
    "actually",
    "instead",
    "should be",
    "correct it",
    "update",
    "fix",
    "wrong",
    "first name",
    "last name",
    "email",
    "correction",
)


def _is_confirmation(text: str) -> bool:
    lowered = text.strip().lower()
    if not lowered:
        return True
    return not any(signal in lowered for signal in _CORRECTION_SIGNALS)


def make_web_pre_save_hook(session: SessionState):
    def pre_save_hook() -> Optional[str]:
        # Blocks this session's background thread, not the HTTP request thread -
        # the frontend keeps polling /api/session/{id} while this waits.
        session.log.append("About to save. Say a correction now, or confirm to continue.")
        session.status = "awaiting_correction"
        correction = session.correction_queue.get()
        session.status = "running"
        if correction and _is_confirmation(correction):
            # "Everything is correct" said by voice is not a correction to apply -
            # without this, it gets fed back as a new user turn, the model tries to
            # save again, and pre_save_hook pauses a second time for the exact same
            # reason, forever, because the only non-voice way to say "go ahead" was
            # the dedicated Confirm button.
            session.log.append(f"Heard confirmation (not a correction): {correction!r}")
            return None
        if correction:
            session.log.append(f"Heard correction: {correction!r}")
        return correction

    return pre_save_hook


def make_web_clarify_hook(session: SessionState):
    def clarify_hook(question: str) -> str:
        # Same blocking-queue mechanism as pre_save_hook, reused for "the model
        # needs more info before it can act at all" instead of "about to save".
        session.log.append(question)
        session.status = "awaiting_correction"
        reply = session.correction_queue.get()
        session.status = "running"
        if reply:
            session.log.append(f"Heard: {reply!r}")
            return reply
        session.log.append("Heard nothing usable - letting the model continue as best it can.")
        return "(no response heard, please proceed with your best interpretation or ask again)"

    return clarify_hook


def run_session(session_id: str, audio_path: str) -> None:
    session = SESSIONS[session_id]
    try:
        text, confidence = transcribe(audio_path)
        session.log.append(f"Recognized: {text!r} (confidence={confidence:.2f})")
        if not text:
            session.status, session.result = "error", {"error": "nothing recognized"}
            return

        client = build_client(os.environ.get("LLM_BACKEND", "groq"))
        crm = CrmBrowser(headless=True)
        try:
            crm.login()
            crm.open_contact_form()
            state = run_agent(
                client,
                crm,
                text,
                narrate_fn=make_web_narrate(session),
                pre_save_hook=make_web_pre_save_hook(session),
                clarify_hook=make_web_clarify_hook(session),
            )
            if not state["record_id"]:
                session.status, session.result = "error", {"message": state["final_message"]}
                return

            filled = state["filled"]
            # Same discipline as the CLI stages: verify by reopening the record,
            # never take the model's own claim of success at face value.
            checks = crm.verify_saved_contact(
                state["record_id"], filled["firstName"], filled["lastName"], filled["email"]
            )
            ok = all(checks.values())
            session.log.append(state["final_message"] or "")
            session.status = "done"
            session.result = {
                "ok": ok,
                "record_id": state["record_id"],
                "checks": checks,
                "record_url": f"https://demo.us.espocrm.com/#Contact/view/{state['record_id']}",
                "contacts_list_url": "https://demo.us.espocrm.com/#Contact",
            }
        finally:
            crm.close()
    except Exception as exc:  # noqa: BLE001 - surface any failure to the frontend instead of a silent hang
        session.status, session.result = "error", {"error": str(exc)}
    finally:
        try:
            os.remove(audio_path)
        except OSError:
            pass


@app.post("/api/session")
async def start_session(audio: UploadFile = File(...)):
    session_id = uuid.uuid4().hex
    SESSIONS[session_id] = SessionState()

    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
        tmp.write(await audio.read())
        audio_path = tmp.name

    thread = threading.Thread(target=run_session, args=(session_id, audio_path), daemon=True)
    thread.start()
    return {"session_id": session_id}


@app.get("/api/session/{session_id}")
async def get_session(session_id: str):
    session = SESSIONS.get(session_id)
    if session is None:
        return JSONResponse({"error": "unknown session"}, status_code=404)
    return {"status": session.status, "log": session.log, "result": session.result}


@app.post("/api/session/{session_id}/correction")
async def post_correction(
    session_id: str, audio: Optional[UploadFile] = File(None), text: Optional[str] = Form(None)
):
    session = SESSIONS.get(session_id)
    if session is None:
        return JSONResponse({"error": "unknown session"}, status_code=404)

    correction_text = None
    if text:
        # Typed fallback bypasses STT entirely - for when voice keeps mangling
        # something like an email address, after a couple of failed attempts.
        correction_text = text
        session.log.append(f"Typed: {text!r}")
    elif audio is not None:
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
            tmp.write(await audio.read())
            audio_path = tmp.name
        text, confidence = transcribe(audio_path)
        os.remove(audio_path)
        if text:
            correction_text = text
            session.log.append(f"Correction recognized: {text!r} (confidence={confidence:.2f})")

    session.correction_queue.put(correction_text)
    return {"ok": True}


@app.post("/api/transcribe")
async def transcribe_only(audio: UploadFile = File(...)):
    """Plain speech-to-text, no agent/session logic - used by the two-part email
    capture, which joins local-part and domain with '@' in code instead of ever
    asking Whisper to recognize the word 'at'."""
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
        tmp.write(await audio.read())
        audio_path = tmp.name
    try:
        text, confidence = transcribe(audio_path)
        return {"text": spoken_to_dotted(text), "confidence": confidence}
    finally:
        os.remove(audio_path)


@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(os.path.dirname(__file__), "static", "index.html")) as f:
        return f.read()
