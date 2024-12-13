import importlib
from functools import wraps
from typing import Any, Callable

import weave
from weave.trace.patcher import MultiPatcher, SymbolPatcher
from weave.trace.op import Op, ProcessedInputs

try:
    from langchain_core.load import dumpd
    from langchain_core.messages import BaseMessage, AIMessage
    from langchain_core.outputs import ChatResult, ChatGeneration
    from langchain_core.tracers import Run
    from langchain_core.tracers.base import BaseTracer
    from langchain_core.tracers.context import register_configure_hook
    from openai.types.chat import ChatCompletion
except ImportError:
    import_failed = True

def chat_nvidia_input_handler(
    func: Any,  # The operation function
    args: tuple,  # Positional arguments
    kwargs: dict,  # Keyword arguments
) -> weave.trace.op.ProcessedInputs:

    # Ensure args contain "self" and "messages"
    if len(args) < 2:
        raise ValueError("Expected at least two arguments: `self` and `messages`.")
    self_object = args[0]
    messages = args[1]

    # Process messages into a format compatible with NVIDIA's API
    if not isinstance(messages, list):
        raise ValueError("Messages must be provided as a list.")
    processed_messages = [
        {"role": "user", "content": msg.get("content", "")}
        if isinstance(msg, dict)
        else {"role": getattr(msg, "type", "user"), "content": getattr(msg, "content", "")}
        for msg in messages
    ]

    # Extract parameters from `self` object
    model = self_object.get("model", "unknown-model")
    base_url = self_object.get("base_url", "https://integrate.api.nvidia.com/v1")
    temperature = self_object.get("temperature")
    max_tokens = self_object.get("max_tokens", 1024)
    top_p = self_object.get("top_p")
    seed = self_object.get("seed")
    stop = kwargs.get("stop", None)

    # Construct the payload
    payload = {
        "model": model,
        "messages": processed_messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "top_p": top_p,
        "seed": seed,
        "stop": stop,
    }

    # Include additional arguments passed via kwargs
    payload.update(kwargs)

    # Preserve the original arguments for debugging or traceability
    original_args = args
    original_kwargs = kwargs
    updated_args = ()  # No positional arguments are changed
    updated_kwargs = {"payload": payload, "base_url": base_url}

    # Create the ProcessedInputs object
    inputs = {
        "self": self_object,
        "messages": messages,
        "stop": stop,
        **kwargs,
    }

    return ProcessedInputs(
        original_args=original_args,
        original_kwargs=original_kwargs,
        args=updated_args,
        kwargs=updated_kwargs,
        inputs=inputs,
    )

def chat_nvidia_post_processor(response) -> ChatCompletion:

    # Extract token usage
    usage = response["llm_output"].get("token_usage", {})

    # Prepare choices
    choices = [
        {
            "index": idx,
            "message": {
                "role": generation["message"].get("role", "assistant"),
                "content": generation["message"].get("content", ""),
            },
            "finish_reason": response["llm_output"].get("finish_reason", "stop"),
        }
        for idx, generation in enumerate(response["generations"])
    ]

    # Construct ChatCompletion
    chat_completion = ChatCompletion(
        id=response.get("id", "unique-id"),
        object="chat.completion",
        created=response["llm_output"].get("created", 0),
        model=response["llm_output"].get("model_name", "unknown-model"),
        choices=choices,
        usage={
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    )

    return chat_completion.model_dump(exclude_unset=True, exclude_none=True)

def create_wrapper_sync(
    name: str,
) -> Callable[[Callable], Callable]:
    def wrapper(fn: Callable) -> Callable:
        op = weave.op()(fn)
        op.name = name  # type: ignore
        op._set_on_input_handler(chat_nvidia_input_handler)
        op._set_on_output_handler()
        op._set_on_finish_handler(chat_nvidia_post_processor)
        return op

    return wrapper


langchain_chatmodel_nvidia_patcher = MultiPatcher(
    [
        SymbolPatcher(
            lambda: importlib.import_module("langchain_nvidia_ai_endpoints"),
            "ChatNVIDIA._generate",
            create_wrapper_sync(name="langchain_nvidia_ai_endpoints.ChatNVIDIA.invoke"),
        )
    ]
)