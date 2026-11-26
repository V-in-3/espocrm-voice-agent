# espocrm-voice-agent

Voice-driven agent that operates the EspoCRM public demo (`demo.us.espocrm.com`)
through a browser. Each stage below is runnable on its own.

## Technical notes

[Open the technical notes PDF](./technical-notes.pdf) for the stack, agent flow,
and how the agent reads and interacts with the CRM interface.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium  # or rely on channel="chrome", see browser.py
```

`browser.py` launches the system Google Chrome (`channel="chrome"`) by default, so a
Chromium download usually isn't needed at all.

## Files

- `browser.py` — Playwright CRM navigation, field entry, account selection,
  saving and verification.
- `tools.py` — tool schemas for reading, filling, linking an account and saving.
- `llm_client.py` — `LLMClient` + `MockClient` / `GroqClient` / `OllamaClient`,
  each formats its own assistant-message history shape via `build_assistant_message`.
- `agent.py` — `run_agent`: a LangGraph workflow for model calls, tools, form
  validation, clarification, confirmation and saving. Takes optional hooks:
  `confirm_fn` (human review of a field before it's typed),
  `narrate_fn` (speak each action as it happens), `pre_save_hook` (pause right
  before Save for a correction), `clarify_hook` (the model asks for missing info
  and the conversation continues instead of ending).
- `voice.py` — mic recording, Whisper transcription (`faster-whisper`) with a
  confidence score, spoken-email normalization, `say`-based TTS (CLI only).
- `run_stage1.py` — scripted, no LLM: proves the browser layer alone.
- `run_stage2.py` — tool-calling loop driven by a model, no voice (`MockClient` by
  default, or a real model via `LLM_BACKEND`).
- `run_stage3.py` — adds voice on the CLI: mic in, Whisper out, same `agent.py`
  loop, `say` for output.
- `server.py` + `static/index.html` — a small FastAPI web app: microphone and
  text-to-speech run in the visitor's own browser (no local mic/`say` dependency),
  Whisper and the LLM run server-side. This is the one meant for "open this link
  and talk to it" style testing.
- `docker-compose.yml` — local Ollama server for the `ollama` backend.

## Stage 1: browser layer without LLM or voice

```bash
.venv/bin/python run_stage1.py            # headless
.venv/bin/python run_stage1.py --headed   # watch the browser
```

Creates a contact and verifies the saved record by reopening it. The demo is
shared and may be reset periodically, so every run creates its own record.

## Stage 2: tool-calling agent loop (no voice)

`run_stage2.py` lets a model drive `CrmBrowser` through the tools in `tools.py`.
`LLM_BACKEND` selects the model:

- `mock` (default) — scripted responses, no network call, proves the loop and
  dispatch table work before any real model is involved.
- `groq` — needs `GROQ_API_KEY` (`GROQ_MODEL` defaults to `openai/gpt-oss-120b`).
- `ollama` — needs a local Ollama server. See the Docker section below; the
  backend starts the container and pulls the model itself if either is missing.

```bash
.venv/bin/python run_stage2.py
LLM_BACKEND=groq .venv/bin/python run_stage2.py
LLM_BACKEND=ollama .venv/bin/python run_stage2.py
```

Whatever the model claims, the script independently reopens the saved record and
reads it back before printing `RESULT: OK` - the model's own "done" message is
never trusted on its own.

The graph requires first name, last name and a valid-looking email before saving.
It can also fill a phone number and the supplied address components, for example
city `Boston` and country `United States`. An existing account can be linked by its
full name; similar matches require user clarification, and duplicate exact names
cannot be selected automatically. The user can explicitly skip the account.
The agent does not create accounts. Filled optional fields
and selected account names are included in the post-save UI verification.

### Updating contact details and linking an account

The agent creates a new contact; it does not edit an existing saved contact.
Before saving, it can:

- Fill or correct the first name, last name, email, phone, street, city,
  state/region, postal code and country. First name, last name and an email
  with a valid-looking format are required; the other fields are optional.
- Ask for unclear or missing information. At the pre-save pause, use **Reply
  by voice** or type a correction, for example: "Change the last name to
  Wilson and the email to emily dot wilson at example dot com." The agent
  applies the correction and asks again before saving.
- Link the contact to an **existing** account, for example: "Link the contact
  to the account Example Company." Provide its full name. If the search returns
  several similar accounts, reply with only the exact account name. If it is
  missing or multiple accounts share that exact name, provide another name or
  say "skip account"; saving remains blocked until the choice is resolved.
- Replace an already selected account with another existing account, or
  explicitly ask to remove the account. Skipping an unresolved account also
  removes any previously selected account. The agent links at most one account
  and does not create or edit accounts.
- Review the form and say "yes" or click **Confirm and save**. The browser
  reads the form before saving, then reopens the saved record and checks its
  visible text for the entered values and account name. Check the resulting
  link yourself as well; speech recognition and text-based verification are
  not guarantees that the intended information was captured.

## Stage 3: voice on the CLI

```bash
LLM_BACKEND=groq VOICE_SECONDS=12 .venv/bin/python run_stage3.py
```

Records from the system microphone, transcribes with Whisper, runs the same
`agent.py` loop, speaks each step (`say`), pauses before Save for a spoken
correction, and asks out loud if required info is missing. `mock` is not a valid
backend here - a real model is needed to interpret a real transcript.

## Stage 4: web app (microphone in the browser)

```bash
export GROQ_API_KEY=...
.venv/bin/uvicorn server:app --reload
```

Open `http://localhost:8000`. The page:

- Generates a fresh example name/email pair on every load and includes an
  optional phone number and Boston, United States address (English names - a
  Ukrainian surname like "Petrenko" is noticeably harder for the small Whisper
  model to transcribe correctly than "Smith" or "Baker").
- Push-to-talk: click **Start talking** once to begin recording, click the same
  button again to stop - no fixed recording duration to guess.
- Speaks each step out loud via the browser's own `speechSynthesis` as the agent
  works (`fill_field`, `save`), so you can follow along as it navigates the CRM.
- Pauses right before Save ("About to save...") for a spoken correction. Explicit
  confirmations such as "yes", "continue", "confirm to continue", "everything is good", and
  "please save it" proceed; other non-empty replies are treated as corrections.
  An empty reply does not confirm saving; the user is prompted again. The
  **Confirm and save** button is available during the pre-save pause.
- If the model is missing required info, it asks out loud and the conversation
  continues (`clarify_hook`) instead of ending the whole request.
- A text field is also available everywhere a voice reply is expected, as a
  fallback for when Whisper keeps mangling the same phrase.
- On completion, links to the saved record and to the Contacts list, so the
  result can be checked on screen, not just taken on faith from the JSON.

If saving cannot be confirmed, the error includes any available CRM response
status and visible alert. The outcome may be unknown even after a timeout:
check the CRM Contacts list before trying again to avoid creating a duplicate.
The agent does not automatically retry the save.

### Known limitation: spoken emails

The base Whisper model sometimes merges `"name at domain.com"` into one run-on
token with no spaces, which no amount of text normalization can recover - there
is no literal "at" left to turn into "@". Mitigations in place:

- `voice.normalize_spoken_email` converts clearly-spoken "at"/"dot" into `@`/`.`
  when Whisper does report them as separate words.
- The LLM itself is often able to parse alternate phrasings that avoid the word
  "at" altogether (e.g. "email is john dot smith domain is example dot com").
- The pause-before-save and clarify-hook loops let the user review or correct
  uncertain information, but cannot detect every transcription error.
- A typed fallback is always available as a last resort.

## Docker (Ollama backend)

```bash
docker volume create espocrm-ollama-data   # first time only
docker compose up -d                        # or just run with LLM_BACKEND=ollama -
                                              # OllamaClient creates the volume,
                                              # starts the container and pulls the
                                              # model itself if any piece is missing
```

The model isn't baked into an image - it lives in the named volume
`espocrm-ollama-data`, kept separate from any other project's Ollama container.
