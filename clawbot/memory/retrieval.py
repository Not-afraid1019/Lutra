"""Memory retrieval — keyword matching + importance scoring."""

import logging
import re

log = logging.getLogger("clawbot.memory.retrieval")


class MemoryRetriever:
    def __init__(self, store, config):
        self._store = store
        self._limit = config.memory_inject_limit

    def retrieve(self, chat_id: str, query_text: str = "") -> list:
        """Retrieve relevant memories for a chat session."""
        results = []
        seen_ids: set[str] = set()

        def _add(rows):
            for m in rows:
                mid = m["id"]
                if mid not in seen_ids:
                    seen_ids.add(mid)
                    results.append(m)

        # 1 — latest session summary for this chat
        _add(
            self._store.get_memories(
                chat_id=chat_id,
                memory_type="session_summary",
                limit=1,
            )
        )

        # 2 — keyword search
        if query_text:
            for kw in self._extract_keywords(query_text):
                _add(self._store.search_by_keyword(kw, limit=5))

        # 3 — recent compressions
        _add(
            self._store.get_memories(
                chat_id=chat_id,
                memory_type="context_compression",
                limit=3,
            )
        )

        results.sort(key=lambda m: m["created_at"], reverse=True)
        results.sort(key=lambda m: m["importance"], reverse=True)
        return results[: self._limit]

    def retrieve_and_format(self, chat_id: str, query_text: str = "") -> str:
        memories = self.retrieve(chat_id, query_text)
        if not memories:
            return ""
        return self._format(memories)

    @staticmethod
    def _extract_keywords(text: str) -> list[str]:
        words = re.split(r"[\s,，。！？、；：\"\"''（）()\[\]【】]+", text)
        return [w for w in words if len(w) >= 2][:5]

    @staticmethod
    def _format(memories: list) -> str:
        labels = {
            "session_summary": "上次会话摘要",
            "context_compression": "对话记录",
        }
        parts = ["[历史记忆]"]
        for m in memories:
            label = labels.get(m["memory_type"], m["memory_type"])
            content = m["content"]
            if len(content) > 500:
                content = content[:500] + "…"
            parts.append(f"\n### {label}\n{content}")
        return "\n".join(parts)
