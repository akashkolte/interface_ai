"""LLM access, isolated behind one small interface.

Kept deliberately thin. The agent loop should depend on "give me a structured
action" rather than on a vendor SDK, so that swapping provider -- or stubbing
the model entirely in tests -- touches this file and nothing else.

Runs against Claude on Amazon Bedrock. Bedrock needs the cross-region inference
profile id (the `us.` prefix); the bare `anthropic.*` ids require provisioned
throughput and fail on-demand, which is a confusing enough error to be worth
naming here.
"""

from __future__ import annotations

import os
from typing import Any

DEFAULT_MODEL = os.environ.get(
    "CUA_MODEL", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)
DEFAULT_REGION = os.environ.get("AWS_REGION", "us-east-1")


class LLMUnavailable(RuntimeError):
    """Raised when no model can be reached, so callers can degrade explicitly."""


class BedrockClient:
    """Structured tool-use calls against Claude on Bedrock."""

    def __init__(self, model: str = DEFAULT_MODEL, region: str = DEFAULT_REGION) -> None:
        self.model = model
        self.region = region
        try:
            from anthropic import AnthropicBedrock
        except ImportError as exc:  # pragma: no cover
            raise LLMUnavailable("anthropic[bedrock] is not installed") from exc
        self._client = AnthropicBedrock(aws_region=region)
        self.input_tokens = 0
        self.output_tokens = 0

    def choose(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tool: dict[str, Any],
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        """Make one decision. Returns the tool input the model chose.

        `tool_choice` forces the tool, so the loop always receives a structured
        action rather than prose it would have to parse.
        """
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=[tool],
                tool_choice={"type": "tool", "name": tool["name"]},
            )
        except Exception as exc:
            raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc

        self.input_tokens += response.usage.input_tokens
        self.output_tokens += response.usage.output_tokens
        for block in response.content:
            if block.type == "tool_use":
                return dict(block.input)
        raise LLMUnavailable("model returned no tool_use block")

    def describe(self) -> str:
        return f"{self.model} (bedrock:{self.region})"
