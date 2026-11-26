import math
import re
import subprocess
import sys

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000

_whisper_model = None

# Matches spoken email shapes like "ivan dot petrenko at example dot com".
# Whisper (any STT, really) reports "at"/"dot" as the literal words, especially
# on short utterances with no surrounding sentence - this is a standard
# normalization step for voice email entry, not something a bigger model alone
# reliably fixes (it still won't catch "at" misheard as a different word
# entirely, e.g. "Add" - that needs more context in the spoken phrase instead).
_LOCAL_PART = r"[\w]+(?:\s*(?:dot|\.)\s*[\w]+)*"
_DOMAIN_PART = r"[\w-]+(?:\s*(?:dot|\.)\s*[\w-]+)+"  # requires at least one dot/"." so "at this" never matches
_SPOKEN_EMAIL_RE = re.compile(rf"({_LOCAL_PART})\s+at\s+({_DOMAIN_PART})", re.IGNORECASE)


def _to_dots(fragment: str) -> str:
    fragment = re.sub(r"\s*dot\s*", ".", fragment, flags=re.IGNORECASE)
    return re.sub(r"\s*\.\s*", ".", fragment)


def normalize_spoken_email(text: str) -> str:
    def repl(match: "re.Match") -> str:
        return f"{_to_dots(match.group(1))}@{_to_dots(match.group(2))}"

    return _SPOKEN_EMAIL_RE.sub(repl, text)


def spoken_to_dotted(text: str) -> str:
    """Converts spoken 'dot' to '.' in a fragment that is already known to be one
    side of an email (no 'at' involved) - used when the local-part and domain are
    captured as two separate recordings instead of asking Whisper for 'at' at all."""
    return _to_dots(text.strip())


def record_audio(seconds: float = 5.0, cue: bool = True) -> np.ndarray:
    if cue:
        # A short countdown gives the person speaking a few seconds of lead time
        # instead of one word that can land before they're paying attention.
        speak("Get ready to speak in 3, 2, 1")
    print(f"[voice] recording for {seconds:.0f}s, speak now...", file=sys.stderr)
    audio = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="float32")
    sd.wait()
    print("[voice] recording done", file=sys.stderr)
    return audio.flatten()


def transcribe(audio: np.ndarray, model_size: str = "base") -> tuple:
    """Returns (text, confidence). confidence is derived from Whisper's own
    avg_logprob per segment (exp(avg_logprob), roughly 0..1) - the same
    "adaptive routing by confidence" idea as gmdh-predictive-engine's scoring
    pipeline, just computed from what this model already reports, no cross-project
    dependency needed."""
    global _whisper_model
    from faster_whisper import WhisperModel

    if _whisper_model is None:
        print(f"[voice] loading whisper model ({model_size})...", file=sys.stderr)
        _whisper_model = WhisperModel(model_size, device="cpu", compute_type="int8")

    segments = list(_whisper_model.transcribe(audio, language="en")[0])
    text = " ".join(segment.text.strip() for segment in segments).strip()
    text = normalize_spoken_email(text)
    confidence = math.exp(sum(s.avg_logprob for s in segments) / len(segments)) if segments else 0.0
    print(f"[voice] transcript: {text!r} (confidence={confidence:.2f})", file=sys.stderr)
    return text, confidence


def listen(seconds: float = 5.0, model_size: str = "base") -> tuple:
    return transcribe(record_audio(seconds), model_size)


def speak(text: str) -> None:
    subprocess.run(["say", text], check=False)
