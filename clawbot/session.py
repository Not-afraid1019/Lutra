"""Session lifecycle — agent loop with tool execution."""

import json
import logging
import re
import threading
import time
from pathlib import Path

from .config import ClawBotConfig
from .context import ContextManager
from .llm import LLMClient
from .memory.retrieval import MemoryRetriever
from .memory.store import MemoryStore
from .models import Memory, SessionState
from .tools import ToolExecutor

log = logging.getLogger("clawbot.session")

SYSTEM_PROMPT_TEMPLATE = """\
你是 {bot_name}，运行在用户本机的 AI 助手，拥有完整的文件系统访问权限。
当前工作目录：{work_dir}

你可以通过工具读写文件、搜索代码、执行命令，就像用户在终端操作一样。

## 工作原则
- 先理解再修改：动手前先阅读相关代码
- 最小改动：只改必要的部分，不过度工程化
- 清楚说明：改了什么、为什么
- 用中文回复
"""


class SessionManager:
    """Manages chat sessions with agent-loop tool execution."""

    def __init__(
        self,
        config: ClawBotConfig,
        store: MemoryStore,
        llm: LLMClient,
    ):
        self._config = config
        self._store = store
        self._llm = llm
        self._context = ContextManager(config, llm)
        self._retriever = MemoryRetriever(store, config)
        self._work_dir = config.project_dir or str(Path.home())
        self._tools = ToolExecutor(self._work_dir)
        self._system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            bot_name=config.bot_name,
            work_dir=self._work_dir,
        )
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.Lock()
        self._restore_sessions()

    # ==================================================================
    # Restore
    # ==================================================================

    def _restore_sessions(self):
        rows = self._store.load_all_sessions()
        now = time.time()
        restored = 0
        for row in rows:
            try:
                state = SessionState.model_validate_json(row["state_json"])
                if now - state.ts < self._config.session_ttl:
                    self._sessions[state.chat_id] = state
                    restored += 1
                else:
                    self._store.delete_session(row["chat_id"])
            except Exception as e:
                log.warning("Failed to restore session %s: %s", row["chat_id"], e)
        if restored:
            log.info("Restored %d sessions", restored)

    # ==================================================================
    # Public API
    # ==================================================================

    def handle_message(self, chat_id: str, sender_id: str, text: str) -> str:
        """Main entry — called for every user message."""

        # Commands
        cmd = self._try_command(chat_id, text)
        if cmd is not None:
            return cmd

        sess = self._get_or_create(chat_id, text)

        # Agent loop
        reply = self._agent_loop(sess, text)

        self._maybe_persist(sess)
        return reply

    def save_all_sessions(self):
        with self._lock:
            sessions = list(self._sessions.values())
        for sess in sessions:
            self._persist_session(sess)
        log.info("Saved %d sessions", len(sessions))

    def cleanup_expired(self):
        now = time.time()
        with self._lock:
            expired = [
                (k, v)
                for k, v in self._sessions.items()
                if now - v.ts > self._config.session_ttl
            ]
            for k, _ in expired:
                del self._sessions[k]

        for chat_id, sess in expired:
            if sess.messages:
                self._generate_session_summary(sess)
            self._store.delete_session(chat_id)

        if expired:
            log.info("Cleaned up %d expired sessions", len(expired))

    # ==================================================================
    # Session helpers
    # ==================================================================

    def _get_or_create(self, chat_id: str, first_message: str = "") -> SessionState:
        with self._lock:
            sess = self._sessions.get(chat_id)
            if sess and time.time() - sess.ts < self._config.session_ttl:
                return sess

            # New session — inject memories if available
            sess = SessionState(chat_id=chat_id, last_save_time=time.time())
            memory_block = self._retriever.retrieve_and_format(
                chat_id, first_message
            )
            if memory_block:
                sess.messages.append({"role": "user", "content": memory_block})
                sess.messages.append(
                    {
                        "role": "assistant",
                        "content": "好的，我已了解之前的对话背景。请告诉我你需要什么帮助。",
                    }
                )

            self._sessions[chat_id] = sess
            return sess

    # ==================================================================
    # Agent loop
    # ==================================================================

    def _agent_loop(self, sess: SessionState, text: str) -> str:
        """Run the tool-use agent loop until Claude gives a text response."""

        sess.messages.append({"role": "user", "content": text})
        sess.ts = time.time()

        for i in range(self._config.max_tool_rounds):
            try:
                resp = self._llm.chat(
                    self._system_prompt,
                    sess.messages,
                    tools=self._tools.definitions,
                )
            except Exception as e:
                log.error("LLM call failed: %s", e)
                return f"抱歉，AI 服务暂时不可用: {e}"

            # Append assistant response
            sess.messages.append({"role": "assistant", "content": resp.content})
            sess.last_input_tokens = resp.input_tokens

            # If Claude finished (no more tool calls), return text
            if resp.stop_reason != "tool_use":
                # Check context compression after the final reply
                if self._context.should_compress(resp.input_tokens):
                    log.info("Tokens %d > %d, compressing…",
                             resp.input_tokens, self._config.context_threshold)
                    summary = self._context.compress(sess)
                    if summary:
                        self._save_compression_memory(sess, summary)
                return resp.text

            # Execute each tool call
            tool_results = []
            for block in resp.content:
                if block.get("type") != "tool_use":
                    continue
                name = block["name"]
                inputs = block["input"]
                log.info(
                    "[TOOL] %s(%s)",
                    name,
                    json.dumps(inputs, ensure_ascii=False)[:200],
                )

                output = self._tools.execute(name, inputs)
                log.info("[TOOL] result: %s", output[:200] + "…" if len(output) > 200 else output)

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": output,
                    }
                )

            sess.messages.append({"role": "user", "content": tool_results})

        return "（达到工具调用次数上限，请把任务拆小一些）"

    # ==================================================================
    # Persistence
    # ==================================================================

    def _maybe_persist(self, sess: SessionState):
        sess.message_count_since_save += 1
        now = time.time()
        if (
            sess.message_count_since_save >= self._config.session_save_messages
            or now - sess.last_save_time >= self._config.session_save_interval
        ):
            self._persist_session(sess)

    def _persist_session(self, sess: SessionState):
        try:
            self._store.save_session(sess.chat_id, sess.model_dump_json())
            sess.message_count_since_save = 0
            sess.last_save_time = time.time()
        except Exception as e:
            log.error("Failed to persist session %s: %s", sess.chat_id, e)

    # ==================================================================
    # Memory
    # ==================================================================

    def _save_compression_memory(self, sess: SessionState, summary: str):
        keywords = self._extract_keywords_from_summary(summary)
        memory = Memory(
            chat_id=sess.chat_id,
            memory_type="context_compression",
            content=summary,
            keywords=keywords,
            message_count=len(sess.messages),
        )
        self._store.save_memory(memory)
        log.info("Saved compression memory for %s", sess.chat_id)

    def _generate_session_summary(self, sess: SessionState):
        if not sess.messages:
            return
        try:
            from .context import _to_text_messages

            text_msgs = _to_text_messages(sess.messages)
            summary = self._llm.summarize(text_msgs)
            keywords = self._extract_keywords_from_summary(summary)
            memory = Memory(
                chat_id=sess.chat_id,
                memory_type="session_summary",
                content=summary,
                keywords=keywords,
                message_count=len(sess.messages),
            )
            self._store.save_memory(memory)
            log.info("Saved session summary for %s", sess.chat_id)
        except Exception as e:
            log.error("Failed to generate session summary: %s", e)

    @staticmethod
    def _extract_keywords_from_summary(summary: str) -> str:
        m = re.search(r"## 关键词\s*\n(.+?)(?:\n##|\Z)", summary, re.DOTALL)
        return m.group(1).strip() if m else ""

    # ==================================================================
    # Commands
    # ==================================================================

    def _try_command(self, chat_id: str, text: str) -> str | None:
        stripped = text.strip()

        if stripped in ("/reset", "/重置"):
            return self._cmd_reset(chat_id)

        if stripped in ("/recall", "/回忆"):
            return self._cmd_recall(chat_id)

        return None

    def _cmd_reset(self, chat_id: str) -> str:
        with self._lock:
            sess = self._sessions.pop(chat_id, None)

        if sess and sess.messages:
            self._generate_session_summary(sess)

        self._store.delete_session(chat_id)
        return "会话已重置。有什么可以帮你的？"

    def _cmd_recall(self, chat_id: str) -> str:
        memories = self._store.get_memories(chat_id=chat_id, limit=10)
        if not memories:
            return "暂无历史记忆。"

        parts = ["历史记忆：\n"]
        for m in memories:
            content = m["content"]
            if len(content) > 200:
                content = content[:200] + "…"
            parts.append(f"- [{m['memory_type']}] {content}")
        return "\n".join(parts)
