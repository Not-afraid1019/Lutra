"""Claude API wrapper — chat (with tool_use), summarize."""

import logging
from dataclasses import dataclass, field

import anthropic

from .config import LutraConfig

log = logging.getLogger("lutra.llm")


@dataclass
class ChatResponse:
    """Wraps a Claude API response."""

    content: list[dict]  # raw content blocks as dicts
    text: str  # concatenated text blocks
    input_tokens: int
    output_tokens: int
    stop_reason: str  # "end_turn" | "tool_use" | "max_tokens"


class LLMClient:
    def __init__(self, config: LutraConfig):
        kwargs: dict = {"api_key": config.claude_api_key}
        if config.claude_base_url:
            kwargs["base_url"] = config.claude_base_url
        self._client = anthropic.Anthropic(**kwargs)
        self._model = config.claude_model
        self._max_tokens = config.claude_max_tokens
        log.info("LLM client ready: model=%s", self._model)

    @property
    def model(self) -> str:
        return self._model

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def chat(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        """Send messages to Claude, optionally with tools."""
        kwargs: dict = {
            "model": self._model,
            "max_tokens": max_tokens or self._max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        resp = self._client.messages.create(**kwargs)

        # Convert content blocks to plain dicts
        content: list[dict] = []
        text_parts: list[str] = []
        for block in resp.content:
            if block.type == "text":
                content.append({"type": "text", "text": block.text})
                text_parts.append(block.text)
            elif block.type == "tool_use":
                content.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )

        return ChatResponse(
            content=content,
            text="\n".join(text_parts),
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            stop_reason=resp.stop_reason,
        )

    def summarize(self, messages: list[dict]) -> str:
        """Summarize a conversation into a structured summary."""
        system = (
            "你是一个对话摘要助手。请将以下对话内容压缩为结构化摘要。\n\n"
            "输出格式：\n"
            "## 叙事概要\n"
            "（用 2-3 段话概括对话的主要内容和发展）\n\n"
            "## 关键事实\n"
            "（列出重要的事实、设定、约定等，用列表格式）\n\n"
            "## 关键词\n"
            "（提取 5-10 个关键词，逗号分隔）"
        )
        # Ensure messages end with a user message (required by some providers)
        if messages and messages[-1].get("role") != "user":
            messages = messages + [{"role": "user", "content": "请总结以上对话。"}]
        resp = self.chat(system, messages, max_tokens=2048)
        return resp.text
