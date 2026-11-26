import json
from typing import Callable, Optional

from browser import CrmBrowser
from llm_client import LLMClient
from tools import TOOLS

ConfirmFn = Callable[[str, str], str]
NarrateFn = Callable[[str, dict], None]
PreSaveHook = Callable[[], Optional[str]]
ClarifyHook = Callable[[str], str]

SYSTEM_PROMPT = (
    "You control an EspoCRM contact form through tools. "
    "Call fill_field for firstName, lastName and email, then call save. "
    "Call one tool at a time and wait for its result before the next call. "
    "When save succeeds, reply with plain text confirming the record id and stop calling tools. "
    "The request may come from a voice transcript and can be incomplete or garbled. "
    "If firstName, lastName or email is not clearly stated in the request, do not "
    "invent or guess a placeholder value for it (never make up names like 'John Doe' "
    "or domains like '@domain.com'). Instead, call no tools and reply with plain text "
    "asking the user to repeat the missing or unclear field. "
    "The user may interrupt with a correction right before you save (for example a "
    "different last name) - it arrives as a new user message. Apply it with fill_field "
    "before calling save again, and briefly state in your next reply what you changed."
)

MAX_STEPS = 20


def dispatch(crm: CrmBrowser, name: str, args: dict, state: dict, confirm_fn: Optional[ConfirmFn] = None) -> str:
    if name == "read_screen":
        return crm.read_screen()
    if name == "fill_field":
        value = args["value"]
        if confirm_fn:
            value = confirm_fn(args["field"], value)
        crm.fill_field(args["field"], value)
        # Report back what was actually typed, not what the model originally proposed -
        # keeps the model's context in sync after a human correction.
        return f"filled {args['field']} with '{value}'"
    if name == "save":
        # Read the form straight from the DOM before saving, so verification later
        # has ground truth independent of whatever the model claims it filled in -
        # needed once the fields come from a voice transcript instead of test data.
        state["filled"] = crm.read_form()
        record_id = crm.save()
        state["record_id"] = record_id
        return json.dumps({"record_id": record_id}) if record_id else "FAILED: no record id"
    raise ValueError(f"unknown tool: {name}")


def run_agent(
    client: LLMClient,
    crm: CrmBrowser,
    user_text: str,
    confirm_fn: Optional[ConfirmFn] = None,
    narrate_fn: Optional[NarrateFn] = None,
    pre_save_hook: Optional[PreSaveHook] = None,
    clarify_hook: Optional[ClarifyHook] = None,
) -> dict:
    """Runs the tool-calling loop for one request. Returns a state dict with
    record_id, filled (the form values read right before save) and final_message.

    confirm_fn, if given, is called as confirm_fn(field, value) before each
    fill_field is applied to the browser, and its return value is used instead -
    lets a caller (e.g. the voice stage) ask a human to type a correction when a
    field looks wrong, without stage 2's scripted/mock runs ever being asked anything.

    narrate_fn, if given, is called as narrate_fn(tool_name, args) right before each
    tool actually runs - lets the caller speak what the agent is about to do.

    pre_save_hook, if given, is called with no arguments right before 'save' actually
    runs. If it returns a correction string, 'save' is skipped this round and the
    correction is fed back in as a new user turn instead, so the model can revise the
    form before trying to save again - this is what makes a correction before Save
    land as an understood change rather than a save that already happened.

    clarify_hook, if given, is called as clarify_hook(question) whenever the model
    replies with plain text and no tool calls BEFORE anything has been saved (it is
    asking for missing/unclear info, not announcing a finished save). Its return
    value is fed back in as a new user turn so the same conversation continues
    instead of ending the whole request on the first question."""
    state = {"record_id": None, "filled": None, "final_message": None}
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]

    for _ in range(MAX_STEPS):
        response = client.complete(messages, TOOLS)

        if not response.tool_calls:
            if state["record_id"] is None and clarify_hook:
                messages.append({"role": "assistant", "content": response.content})
                reply = clarify_hook(response.content)
                messages.append({"role": "user", "content": reply})
                continue
            state["final_message"] = response.content
            return state

        messages.append(client.build_assistant_message(response))
        interrupted = False
        for tc in response.tool_calls:
            if tc.name == "save" and pre_save_hook:
                correction = pre_save_hook()
                if correction:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": tc.name,
                            "content": "save postponed: the user spoke up with a correction first",
                        }
                    )
                    messages.append({"role": "user", "content": correction})
                    interrupted = True
                    break

            if narrate_fn:
                narrate_fn(tc.name, tc.arguments)

            result = dispatch(crm, tc.name, tc.arguments, state, confirm_fn)
            print(f"tool {tc.name}({tc.arguments}) -> {result}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": str(result)})

        if interrupted:
            continue

    raise RuntimeError("agent did not finish within the step limit")
