import json
import os
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
_OLLAMA_VOLUME = "espocrm-ollama-data"
_OLLAMA_CONTAINER = "espocrm-ollama"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    content: Optional[str]
    tool_calls: List[ToolCall] = field(default_factory=list)


class LLMClient(ABC):
    @abstractmethod
    def complete(self, messages: list, tools: list) -> LLMResponse:
        ...

    def build_assistant_message(self, response: LLMResponse) -> dict:
        # OpenAI/Groq style: each tool call's arguments is a JSON-encoded string.
        return {
            "role": "assistant",
            "content": response.content,
            "tool_calls": [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                for tc in response.tool_calls
            ],
        }


class MockClient(LLMClient):
    """Returns a pre-scripted sequence of responses, one per call. No network, no model."""

    def __init__(self, script: List[LLMResponse]):
        self._script = list(script)

    def complete(self, messages: list, tools: list) -> LLMResponse:
        if not self._script:
            raise RuntimeError("MockClient script exhausted before the agent finished")
        return self._script.pop(0)


class GroqClient(LLMClient):
    def __init__(self, model: Optional[str] = None):
        from groq import Groq

        self._client = Groq(api_key=os.environ["GROQ_API_KEY"])
        self._model = model or os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

    def complete(self, messages: list, tools: list) -> LLMResponse:
        resp = self._client.chat.completions.create(
            model=self._model, messages=messages, tools=tools, tool_choice="auto"
        )
        msg = resp.choices[0].message
        calls = [
            ToolCall(id=tc.id, name=tc.function.name, arguments=json.loads(tc.function.arguments))
            for tc in (msg.tool_calls or [])
        ]
        return LLMResponse(content=msg.content, tool_calls=calls)


def _volume_exists(volume: str) -> bool:
    return subprocess.run(["docker", "volume", "inspect", volume], capture_output=True).returncode == 0


def _container_running(container: str) -> bool:
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container], capture_output=True, text=True
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _ensure_container() -> None:
    if not _volume_exists(_OLLAMA_VOLUME):
        print(f"[ollama] volume {_OLLAMA_VOLUME} missing, creating it")
        subprocess.run(["docker", "volume", "create", _OLLAMA_VOLUME], check=True)

    if _container_running(_OLLAMA_CONTAINER):
        return

    print(f"[ollama] container {_OLLAMA_CONTAINER} not running, starting via docker compose")
    subprocess.run(["docker", "compose", "up", "-d"], cwd=_PROJECT_DIR, check=True)

    for _ in range(30):
        if _container_running(_OLLAMA_CONTAINER):
            return
        time.sleep(1)
    raise RuntimeError(f"container {_OLLAMA_CONTAINER} did not reach running state in time")


def _wait_for_server(base_url: str, http_client, timeout: int = 30) -> None:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            if http_client.get(f"{base_url}/api/tags").status_code == 200:
                return
        except Exception as exc:
            last_error = exc
        time.sleep(1)
    raise RuntimeError(f"Ollama server at {base_url} did not become ready in {timeout}s: {last_error}")


def _ensure_model(base_url: str, model: str, http_client) -> None:
    names = {m["name"] for m in http_client.get(f"{base_url}/api/tags").json().get("models", [])}
    if model in names:
        return
    print(f"[ollama] model {model} not found in {_OLLAMA_CONTAINER}, pulling it now (this can take a while)")
    subprocess.run(["docker", "exec", _OLLAMA_CONTAINER, "ollama", "pull", model], check=True)


class OllamaClient(LLMClient):
    """Talks to a local Ollama server (OpenAI-style tool-calling over /api/chat).

    Fully self-contained: on construction it makes sure the volume and container
    from docker-compose.yml exist and are running, and that the requested model
    is pulled, before the first chat call is made.
    """

    def __init__(self, model: Optional[str] = None, base_url: Optional[str] = None):
        import httpx

        self._base_url = base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11435")
        self._model = model or os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
        self._http = httpx.Client(timeout=300.0)

        _ensure_container()
        _wait_for_server(self._base_url, self._http)
        _ensure_model(self._base_url, self._model, self._http)

    def complete(self, messages: list, tools: list) -> LLMResponse:
        resp = self._http.post(
            f"{self._base_url}/api/chat",
            json={"model": self._model, "messages": messages, "tools": tools, "stream": False},
        )
        resp.raise_for_status()
        msg = resp.json()["message"]
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc["function"]
            args = fn["arguments"]
            if isinstance(args, str):
                args = json.loads(args)
            calls.append(ToolCall(id=tc.get("id", f"call_{i}"), name=fn["name"], arguments=args))
        return LLMResponse(content=msg.get("content"), tool_calls=calls)

    def build_assistant_message(self, response: LLMResponse) -> dict:
        # Ollama expects tool_calls[].function.arguments as a native object,
        # not a JSON-encoded string like OpenAI/Groq - confirmed against a live
        # qwen2.5:7b response, round-tripping its own tool_calls shape back fails
        # with "Value looks like object, but can't find closing '}' symbol" otherwise.
        return {
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": [
                {"id": tc.id, "function": {"name": tc.name, "arguments": tc.arguments}}
                for tc in response.tool_calls
            ],
        }


def build_client(backend: str) -> LLMClient:
    if backend == "groq":
        return GroqClient()
    if backend == "ollama":
        return OllamaClient()
    raise ValueError(f"unknown LLM_BACKEND: {backend} (expected groq or ollama; mock is built by the caller)")
