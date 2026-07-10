
import hashlib
import json
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage

from worker import runtime
from worker.config import settings


class BudgetExceeded(RuntimeError):
    """Raised when a run's cumulative token usage crosses its hard budget."""


def make_llm(provider: str, model: str | None = None) -> BaseChatModel:
    """Build a chat model for the requested provider ('openai' or 'gemini')."""
    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model or settings.openai_model, api_key=settings.openai_api_key, temperature=0)
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model or settings.gemini_model, google_api_key=settings.google_api_key, temperature=0
        )
    raise ValueError(f"unsupported provider {provider!r}; expected 'openai' or 'gemini'")


def _hash_messages(messages: list[BaseMessage]) -> str:
    serial = json.dumps(
        [
            {
                "type": m.type,
                "content": m.content,
                "tool_calls": getattr(m, "tool_calls", None),
                "tool_call_id": getattr(m, "tool_call_id", None),
            }
            for m in messages
        ],
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(serial.encode("utf-8")).hexdigest()


def _usage_of(message: AIMessage) -> dict[str, int]:
    usage = message.usage_metadata or {}
    return {
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
    }


def _serialize_ai(message: AIMessage) -> dict:
    return {
        "kind": "ai_message",
        "content": message.content,
        "tool_calls": message.tool_calls,
        "usage": _usage_of(message),
    }


def _deserialize_ai(payload: dict) -> AIMessage:
    return AIMessage(content=payload["content"], tool_calls=payload.get("tool_calls") or [])


def check_budget(total_tokens: int) -> None:
    if total_tokens > settings.max_run_tokens:
        raise BudgetExceeded(f"run exceeded token budget of {settings.max_run_tokens}")


async def cached_call(
    llm: Any,
    messages: list[BaseMessage],
    *,
    run_id: str,
    node: str,
    step_idx: int,
    attempt: int,
    call_idx: int,
    provider: str,
    model: str,
) -> tuple[AIMessage, dict[str, int]]:
    """Invoke a (possibly tool-bound) chat model with ledger replay."""
    prompt_hash = _hash_messages(messages)
    stored = await runtime.ledger.get(run_id, node, step_idx, attempt, call_idx, provider, model, prompt_hash)
    if stored is not None:
        return _deserialize_ai(stored), {"input_tokens": 0, "output_tokens": 0}
    response = await llm.ainvoke(messages)
    usage = _usage_of(response)
    await runtime.ledger.put(
        run_id, node, step_idx, attempt, call_idx, provider, model, prompt_hash, _serialize_ai(response)
    )
    return response, usage


async def cached_structured_call(
    llm: BaseChatModel,
    schema: Any,
    messages: list[BaseMessage],
    *,
    run_id: str,
    node: str,
    step_idx: int,
    attempt: int,
    call_idx: int,
    provider: str,
    model: str,
) -> tuple[Any, dict[str, int]]:
    """Invoke with structured output (native tool-calling on both providers), ledger-replayed."""
    prompt_hash = _hash_messages(messages)
    stored = await runtime.ledger.get(run_id, node, step_idx, attempt, call_idx, provider, model, prompt_hash)
    if stored is not None:
        return schema.model_validate(stored["parsed"]), {"input_tokens": 0, "output_tokens": 0}
    structured = llm.with_structured_output(schema, include_raw=True)
    result = await structured.ainvoke(messages)
    parsed = result["parsed"]
    if parsed is None:
        raise ValueError(f"model returned output not matching schema {schema.__name__}: {result.get('parsing_error')}")
    usage = _usage_of(result["raw"])
    await runtime.ledger.put(
        run_id,
        node,
        step_idx,
        attempt,
        call_idx,
        provider,
        model,
        prompt_hash,
        {"kind": "structured", "parsed": parsed.model_dump(), "usage": usage},
    )
    return parsed, usage
