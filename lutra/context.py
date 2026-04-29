"""Context management — token tracking and conversation compression."""

import json
import logging

log = logging.getLogger("lutra.context")


class ContextManager:
    def __init__(self, config, llm):
        self._threshold = config.context_threshold
        self._keep_recent = config.context_keep_recent
        self._llm = llm

    def should_compress(self, input_tokens: int) -> bool:
        return input_tokens > self._threshold

    def compress(self, session) -> str | None:
        """Compress old messages in-place, keeping recent ones.

        Returns the summary text, or None if nothing to compress.
        """
        messages = session.messages
        keep = self._keep_recent

        if len(messages) <= keep + 2:
            log.info("Not enough messages to compress (%d)", len(messages))
            return None

        # Find a clean split point: recent_messages must start with a user
        # text message, not an orphaned tool_result whose tool_use was in old.
        split = len(messages) - keep
        while split < len(messages):
            msg = messages[split]
            content = msg.get("content", "")
            # Good boundary: user message with plain text content
            if msg.get("role") == "user" and isinstance(content, str):
                break
            # Also good: assistant message (will be preceded by user text)
            if msg.get("role") == "assistant":
                # Back up one to include the user message before it
                if split > 0 and messages[split - 1].get("role") == "user":
                    split -= 1
                break
            split += 1

        if split >= len(messages) - 2:
            log.info("Cannot find clean split point, skipping compression")
            return None

        old_messages = messages[:split]
        recent_messages = messages[split:]

        # Convert structured messages to plain text for summarization
        text_msgs = _to_text_messages(old_messages)

        log.info(
            "Compressing %d messages (keeping %d recent)",
            len(old_messages),
            len(recent_messages),
        )
        summary = self._llm.summarize(text_msgs)

        # Replace with a summary pair
        session.messages = [
            {"role": "user", "content": f"[对话摘要]\n{summary}"},
            {"role": "assistant", "content": "好的，我已了解之前的对话内容。让我们继续。"},
        ] + recent_messages

        log.info(
            "Compression done: %d -> %d messages",
            len(messages),
            len(session.messages),
        )
        return summary


def _to_text_messages(messages: list[dict]) -> list[dict]:
    """Convert messages (possibly with tool_use blocks) to plain text."""
    result = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            result.append({"role": msg["role"], "content": content})
        elif isinstance(content, list):
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    parts.append(block["text"])
                elif btype == "tool_use":
                    inp = json.dumps(block.get("input", {}), ensure_ascii=False)
                    if len(inp) > 100:
                        inp = inp[:100] + "…"
                    parts.append(f"[调用 {block['name']}({inp})]")
                elif btype == "tool_result":
                    r = str(block.get("content", ""))
                    if len(r) > 150:
                        r = r[:150] + "…"
                    parts.append(f"[结果: {r}]")
            if parts:
                result.append({"role": msg["role"], "content": "\n".join(parts)})
    return result
