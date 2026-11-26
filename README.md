# espocrm-voice-agent

Voice-driven agent that operates the EspoCRM public demo (`demo.us.espocrm.com`)
through a browser. Each stage below is runnable on its own.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install playwright groq faster-whisper 'av<19' sounddevice soundfile fastapi uvicorn python-multipart
.venv/bin/python -m playwright install chromium  # or rely on channel="chrome", see browser.py
```

`browser.py` launches the system Google Chrome (`channel="chrome"`) by default, so a
Chromium download usually isn't needed at all.

## Files

- `browser.py` — `CrmBrowser`: `login`, `read_screen`, `fill_field`, `read_form`,
  `save`, `verify_saved_contact`.
- `tools.py` — OpenAI-style tool schemas for `read_screen`, `fill_field`, `save`.
- `llm_client.py` — `LLMClient` + `MockClient` / `GroqClient` / `OllamaClient`,
  each formats its own assistant-message history shape via `build_assistant_message`.
- `agent.py` — `run_agent`: the shared tool-calling loop used by every stage above 1.
  Takes optional hooks: `confirm_fn` (human review of a field before it's typed),
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

- Generates a fresh example name/email pair on every load (English names - a
  Ukrainian surname like "Petrenko" is noticeably harder for the small Whisper
  model to transcribe correctly than "Smith" or "Baker").
- Push-to-talk: click **Start talking** once to begin recording, click the same
  button again to stop - no fixed recording duration to guess.
- Speaks each step out loud via the browser's own `speechSynthesis` as the agent
  works (`fill_field`, `save`), so you can follow along as it navigates the CRM.
- Pauses right before Save ("About to save...") for a spoken correction. Plain
  confirmations ("yes", "continue", "everything is good", "please save it") are
  told apart from real corrections by looking for correction-signal words
  (change / actually / wrong / first name / last name / email / ...); anything
  without one of those is treated as "no correction, proceed" by default.
- If the model is missing required info, it asks out loud and the conversation
  continues (`clarify_hook`) instead of ending the whole request.
- A text field is also available everywhere a voice reply is expected, as a
  fallback for when Whisper keeps mangling the same phrase.
- On completion, links to the saved record and to the Contacts list, so the
  result can be checked on screen, not just taken on faith from the JSON.

### Known limitation: spoken emails

The base Whisper model sometimes merges `"name at domain.com"` into one run-on
token with no spaces, which no amount of text normalization can recover - there
is no literal "at" left to turn into "@". Mitigations in place:

- `voice.normalize_spoken_email` converts clearly-spoken "at"/"dot" into `@`/`.`
  when Whisper does report them as separate words.
- The LLM itself is often able to parse alternate phrasings that avoid the word
  "at" altogether (e.g. "email is john dot smith domain is example dot com").
- The pause-before-save and clarify-hook loops mean a bad transcription leads to
  the agent asking again, not to a wrong value getting silently saved.
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
