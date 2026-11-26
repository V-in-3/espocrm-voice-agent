import re
from typing import Callable, Optional, Protocol, TypedDict

from langgraph.graph import END, StateGraph

from llm_client import LLMClient, LLMResponse, ToolCall
from tools import TOOLS

ConfirmFn = Callable[[str, str], str]
NarrateFn = Callable[[str, dict], None]
PreSaveHook = Callable[[], Optional[str]]
ClarifyHook = Callable[[str], str]

SYSTEM_PROMPT = (
    "You control an EspoCRM contact form through tools. "
    "Fill firstName, lastName and email, then save. "
    "Also fill phone and address fields when the user supplies them. "
    "When the user requests an existing account, call link_account with its full name. "
    "If the user explicitly asks to remove the account, call clear_account. "
    "Never invent names, emails, addresses, phone numbers, or accounts. "
    "If required information is unclear, ask the user to clarify. "
    "If an account cannot be linked, ask the user for another name or whether to skip it. "
    "Call one tool at a time and wait for its result. "
    "A correction before saving arrives as a new user message; apply it before saving."
)
MAX_STEPS = 20
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SKIP_ACCOUNT_RE = re.compile(
    r"^(?:please\s+)?(?:skip(?:\s+(?:the\s+)?account)?|"
    r"no account|without (?:an? )?account)(?:\s+please)?[.!]?$",
    re.IGNORECASE,
)


class CrmActions(Protocol):
    def read_screen(self) -> str: ...
    def fill_field(self, field: str, value: str) -> None: ...
    def read_form(self) -> dict: ...
    def link_account(self, name: str, confirmed: bool = False) -> tuple[bool, str]: ...
    def clear_accounts(self) -> None: ...
    def save(self) -> Optional[str]: ...


class AgentState(TypedDict):
    messages: list
    response: Optional[LLMResponse]
    steps: int
    route: str
    question: str
    account_pending: bool
    confirmed_account: str
    save_call: Optional[ToolCall]
    save_after: list
    filled: Optional[dict]
    record_id: Optional[str]
    final_message: Optional[str]


def run_agent(
    client: LLMClient,
    crm: CrmActions,
    user_text: str,
    confirm_fn: Optional[ConfirmFn] = None,
    narrate_fn: Optional[NarrateFn] = None,
    pre_save_hook: Optional[PreSaveHook] = None,
    clarify_hook: Optional[ClarifyHook] = None,
) -> dict:
    """Run a bounded LangGraph workflow for one contact creation request."""

    def tool_result(call: ToolCall, content: str) -> dict:
        return {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content}

    def model(state: AgentState) -> dict:
        if state["steps"] >= MAX_STEPS:
            raise RuntimeError("agent did not finish within the step limit")
        response = client.complete(state["messages"], TOOLS)
        if not response.tool_calls:
            if clarify_hook:
                question = response.content or "Please clarify your contact request."
                return {"response": response, "steps": state["steps"] + 1,
                        "route": "clarify", "question": question}
            return {"steps": state["steps"] + 1, "route": "end",
                    "final_message": response.content}
        return {
            "response": response,
            "steps": state["steps"] + 1,
            "messages": state["messages"] + [client.build_assistant_message(response)],
            "route": "tools",
        }

    def tools(state: AgentState) -> dict:
        response = state["response"]
        if response is None:
            raise RuntimeError("missing model response")
        messages = list(state["messages"])
        account_pending = state["account_pending"]
        confirmed_account = state["confirmed_account"]
        for index, call in enumerate(response.tool_calls):
            if call.name == "save":
                return {"messages": messages, "save_call": call,
                        "save_after": response.tool_calls[index + 1:],
                        "account_pending": account_pending, "confirmed_account": confirmed_account,
                        "route": "validate"}
            if call.name == "read_screen":
                result = crm.read_screen()
            elif call.name == "fill_field":
                field, value = call.arguments["field"], call.arguments["value"]
                if confirm_fn:
                    value = confirm_fn(field, value)
                if narrate_fn:
                    narrate_fn(call.name, {"field": field, "value": value})
                crm.fill_field(field, value)
                result = f"filled {field} with {value!r}"
            elif call.name == "link_account":
                name = call.arguments["name"]
                confirmed = bool(confirmed_account and confirmed_account.casefold() == name.strip().casefold())
                if account_pending and not confirmed:
                    result = "Please reply with only the exact account name or say skip account."
                    messages.append(tool_result(call, result))
                    for remaining in response.tool_calls[index + 1:]:
                        messages.append(tool_result(remaining, "Not executed: account needs clarification."))
                    return {"messages": messages, "account_pending": True, "confirmed_account": "",
                            "route": "clarify", "question": result}
                linked, result = crm.link_account(name, confirmed=confirmed)
                confirmed_account = ""
                account_pending = not linked
                if linked and narrate_fn:
                    narrate_fn(call.name, call.arguments)
            elif call.name == "clear_account":
                if account_pending:
                    result = "Account needs clarification. The user must explicitly say skip account."
                    messages.append(tool_result(call, result))
                    for remaining in response.tool_calls[index + 1:]:
                        messages.append(tool_result(remaining, "Not executed: account needs clarification."))
                    return {"messages": messages, "account_pending": True, "confirmed_account": "",
                            "route": "clarify", "question": result}
                crm.clear_accounts()
                account_pending = False
                result = "Removed the linked account."
                if narrate_fn:
                    narrate_fn(call.name, call.arguments)
            else:
                raise ValueError(f"unknown tool: {call.name}")
            print(f"tool {call.name}({call.arguments}) -> {result}")
            messages.append(tool_result(call, result))
            if call.name == "link_account" and account_pending:
                for remaining in response.tool_calls[index + 1:]:
                    messages.append(tool_result(remaining, "Not executed: account needs clarification."))
                return {"messages": messages, "account_pending": True, "confirmed_account": "",
                        "route": "clarify", "question": result}
        return {"messages": messages, "account_pending": account_pending,
                "confirmed_account": confirmed_account, "route": "model"}

    def validate(state: AgentState) -> dict:
        call = state["save_call"]
        if call is None:
            raise RuntimeError("missing save request")
        if state["account_pending"]:
            return {"messages": state["messages"] + [tool_result(call, "Save blocked: account needs clarification.")] +
                    [tool_result(c, "Not executed: save was blocked.") for c in state["save_after"]],
                    "route": "clarify", "question": "Please provide another account name or say skip account."}
        filled = crm.read_form()
        missing = [field for field in ("firstName", "lastName", "email") if not filled[field].strip()]
        if filled["email"].strip() and not EMAIL_RE.fullmatch(filled["email"].strip()):
            missing.append("a valid email")
        if missing:
            problem = "Save blocked: provide " + ", ".join(missing) + "."
            return {"messages": state["messages"] + [tool_result(call, problem)] +
                    [tool_result(c, "Not executed: save was blocked.") for c in state["save_after"]], "route": "model"}
        return {"filled": filled, "route": "confirm"}

    def confirm(state: AgentState) -> dict:
        call = state["save_call"]
        if call is None:
            raise RuntimeError("missing save request")
        correction = pre_save_hook() if pre_save_hook else None
        if correction:
            return {"messages": state["messages"] + [
                tool_result(call, "Save postponed for a user correction."),
            ] + [
                tool_result(c, "Not executed: save was postponed.") for c in state["save_after"]
            ] + [
                {"role": "user", "content": correction},
            ], "route": "model"}
        return {"route": "save"}

    def save(state: AgentState) -> dict:
        if state["filled"] is None:
            raise RuntimeError("Cannot save without validated form values.")
        if narrate_fn:
            narrate_fn("save", {})
        record_id = crm.save()
        if not record_id:
            raise RuntimeError("Could not confirm that the contact was saved. Check the CRM contacts list before trying again to avoid duplicates.")
        return {"record_id": record_id, "final_message": "Saved the contact.", "route": "end"}

    def clarify(state: AgentState) -> dict:
        if clarify_hook is None:
            raise RuntimeError(state["question"])
        reply = clarify_hook(state["question"])
        if not isinstance(reply, str) or not reply.strip():
            raise RuntimeError("Clarification was empty.")
        messages = list(state["messages"])
        response = state["response"]
        if response and not response.tool_calls:
            messages.append({"role": "assistant", "content": state["question"]})
        messages.append({"role": "user", "content": reply})
        skip_account = state["account_pending"] and bool(SKIP_ACCOUNT_RE.fullmatch(reply.strip()))
        if skip_account:
            crm.clear_accounts()
        return {"messages": messages, "response": None,
                "account_pending": state["account_pending"] and not skip_account,
                "confirmed_account": reply.strip() if state["account_pending"] and not skip_account else "",
                "route": "model"}

    graph = StateGraph(AgentState)
    graph.add_node("model", model)
    graph.add_node("tools", tools)
    graph.add_node("validate", validate)
    graph.add_node("confirm", confirm)
    graph.add_node("save", save)
    graph.add_node("clarify", clarify)
    graph.set_entry_point("model")
    for node in ("model", "tools", "validate", "confirm"):
        graph.add_conditional_edges(node, lambda state: state["route"], {
            "model": "model", "tools": "tools", "validate": "validate",
            "confirm": "confirm", "save": "save", "clarify": "clarify", "end": END,
        })
    graph.add_edge("clarify", "model")
    graph.add_edge("save", END)
    result = graph.compile().invoke({
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": user_text}],
        "response": None, "steps": 0, "route": "model", "question": "",
        "account_pending": False, "confirmed_account": "", "save_call": None, "save_after": [], "filled": None,
        "record_id": None, "final_message": None,
    }, {"recursion_limit": MAX_STEPS * 5 + 10})
    return {"record_id": result["record_id"], "filled": result["filled"],
            "final_message": result["final_message"]}
