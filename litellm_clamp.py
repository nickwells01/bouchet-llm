"""LiteLLM custom callback to clamp max_tokens and disable Qwen3 thinking."""

import os
import re

from litellm.integrations.custom_logger import CustomLogger

MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "4096"))

# Pattern to strip <think>...</think> blocks from model output (safety net)
THINK_PATTERN = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


class MaxTokensClamper(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        # Clamp output tokens
        if data.get("max_tokens") and data["max_tokens"] > MAX_OUTPUT_TOKENS:
            data["max_tokens"] = MAX_OUTPUT_TOKENS
        if data.get("max_completion_tokens") and data["max_completion_tokens"] > MAX_OUTPUT_TOKENS:
            data["max_completion_tokens"] = MAX_OUTPUT_TOKENS

        # Inject chat_template_kwargs to disable Qwen3 thinking mode.
        # This tells vLLM to render the chat template with enable_thinking=false,
        # which prevents <think>...</think> blocks from being generated.
        extra_body = data.get("extra_body") or {}
        chat_kwargs = extra_body.get("chat_template_kwargs") or {}
        chat_kwargs["enable_thinking"] = False
        extra_body["chat_template_kwargs"] = chat_kwargs
        data["extra_body"] = extra_body

        return data

    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        """Strip <think>...</think> from non-streaming responses as safety net."""
        try:
            if hasattr(response, "choices"):
                for choice in response.choices:
                    msg = getattr(choice, "message", None)
                    if msg and getattr(msg, "content", None):
                        cleaned = THINK_PATTERN.sub("", msg.content)
                        if cleaned != msg.content:
                            msg.content = cleaned
        except Exception:
            pass
        return response


max_tokens_clamper = MaxTokensClamper()
