"""Session lifecycle — agent loop with tool execution."""

import json
import logging
import re
import threading
import time
from pathlib import Path

from .config import LutraConfig
from .context import ContextManager
from .llm import LLMClient
from .memory.retrieval import MemoryRetriever
from .memory.store import MemoryStore
from .models import Memory, SessionState
from .tools import ToolExecutor

log = logging.getLogger("lutra.session")

SYSTEM_PROMPT_TEMPLATE = """\
你是 {bot_name}，运行在用户本机的 AI 助手，拥有完整的文件系统访问权限。
当前工作目录：{work_dir}

你可以通过工具读写文件、搜索代码、执行命令，就像用户在终端操作一样。

## 工作原则
- 先理解再修改：动手前先阅读相关代码
- 最小改动：只改必要的部分，不过度工程化
- 清楚说明：改了什么、为什么
- 用中文回复

## Git 分支规范
- 每次修复任务（JIRA/MR review）必须基于最新的 main 分支新建独立分支
- 步骤：git fetch origin && git checkout main && git pull && git checkout -b fix/xxx
- 不同任务的修复绝不能混在同一个分支上

## JIRA 工作流（严格遵守）
- 用户说"分析"某个 JIRA → 第一步直接调用 jira_analyze，不要先调 jira_get_issue
- 用户说"修复"某个 JIRA → 第一步直接调用 jira_fix，不要先调 jira_get_issue
- jira_get_issue 仅用于"查看/列出"等轻量查询场景
- 严禁绕过 jira_analyze/jira_fix 自行用 read_file/run_command 分析或修复
"""


class SessionManager:
    """Manages chat sessions with agent-loop tool execution."""

    def __init__(
        self,
        config: LutraConfig,
        store: MemoryStore,
        llm: LLMClient,
    ):
        self._config = config
        self._store = store
        self._llm = llm
        self._context = ContextManager(config, llm)
        self._retriever = MemoryRetriever(store, config)
        self._work_dir = config.project_dir or str(Path.home())

        jira_config = None
        if config.jira_server and config.jira_pat:
            jira_config = {
                "server": config.jira_server,
                "pat": config.jira_pat,
                "aegis_cas": config.jira_aegis_cas,
            }

        mimo_config = {
            "api_key": config.mimo_api_key,
            "base_url": config.mimo_base_url,
            "model": config.mimo_model,
            "provider_id": config.mimo_provider_id,
        }

        gitlab_config = None
        if config.gitlab_pat:
            gitlab_config = {
                "url": config.gitlab_url,
                "pat": config.gitlab_pat,
                "project": config.gitlab_project,
            }

        # data_dir: resolve relative to lutra project root (where agent.py lives)
        data_dir = str(Path(__file__).resolve().parent.parent / "data")

        self._tools = ToolExecutor(
            self._work_dir,
            jira_config=jira_config,
            mimo_config=mimo_config,
            data_dir=data_dir,
            project_dir=self._work_dir,
            gitlab_config=gitlab_config,
        )
        self._system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            bot_name=config.bot_name,
            work_dir=self._work_dir,
        )
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.Lock()
        self._chat_locks: dict[str, threading.Lock] = {}
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
        """Main entry — called for every user message.

        Uses per-chat locks to serialize concurrent messages from the same chat.
        """
        # Commands don't need serialization
        cmd = self._try_command(chat_id, text)
        if cmd is not None:
            return cmd

        # Get or create per-chat lock
        with self._lock:
            if chat_id not in self._chat_locks:
                self._chat_locks[chat_id] = threading.Lock()
            chat_lock = self._chat_locks[chat_id]

        # Serialize messages for the same chat
        with chat_lock:
            sess = self._get_or_create(chat_id, text)
            reply = self._agent_loop(sess, text)
            self._maybe_persist(sess)

        return reply

    def update_jira_token(self, aegis_cas: str) -> bool:
        """Hot-update _aegis_cas on the live JIRA session."""
        return self._tools.update_jira_token(aegis_cas)

    def poll_gitlab_reviews(self, feishu_sender=None) -> int:
        """轮询所有 open MR，处理未回复的 review 评论。返回处理数量。"""
        from . import gitlab_client

        cfg = self._config
        if not cfg.gitlab_pat:
            return 0

        session = gitlab_client.connect(cfg.gitlab_url, cfg.gitlab_pat)
        base_url = cfg.gitlab_url
        project_path = cfg.gitlab_project

        # Auto-detect project if not configured
        if not project_path:
            detected_url, detected_path = gitlab_client.detect_project(self._work_dir)
            if detected_path:
                base_url = detected_url
                project_path = detected_path
            else:
                log.warning("[POLL] Cannot detect GitLab project path")
                return 0

        try:
            mrs = gitlab_client.list_open_mrs(
                session, base_url, project_path,
                author_username=cfg.gitlab_bot_username or None,
            )
        except Exception as e:
            log.error("[POLL] Failed to list open MRs: %s", e)
            return 0

        log.info("[POLL] checking %d open MRs...", len(mrs))
        processed = 0

        for mr in mrs:
            mr_iid = mr["iid"]
            try:
                discussions = gitlab_client.list_discussions(
                    session, base_url, project_path, mr_iid,
                )
            except Exception as e:
                log.error("[POLL] MR !%s list_discussions failed: %s", mr_iid, e)
                continue

            for disc in discussions:
                notes = disc.get("notes", [])
                if not notes:
                    continue
                first = notes[0]
                # Only unresolved, resolvable discussions
                if not first.get("resolvable", False):
                    continue
                if first.get("resolved", False):
                    continue
                # Skip if last note is by bot
                last_note = notes[-1]
                last_author = last_note.get("author", {}).get("username", "")
                if cfg.gitlab_bot_username and last_author == cfg.gitlab_bot_username:
                    continue

                disc_id = disc["id"]
                lock_key = f"{mr_iid}:{disc_id}"
                if not self._try_acquire_poll_lock(lock_key):
                    continue

                try:
                    self._process_single_discussion(
                        mr, disc, feishu_sender,
                    )
                    processed += 1
                except Exception as e:
                    log.error("[POLL] MR !%s disc %s failed: %s", mr_iid, disc_id, e)
                finally:
                    self._release_poll_lock(lock_key)

        return processed

    def _process_single_discussion(self, mr: dict, discussion: dict, feishu_sender=None):
        """处理单条 discussion，通过 agent loop 自动回复/修复。"""
        mr_iid = mr["iid"]
        source_branch = mr.get("source_branch", "")
        disc_id = discussion["id"]
        notes = discussion["notes"]
        last_note = notes[-1]

        author = last_note.get("author", {}).get("username", "?")
        comment = last_note.get("body", "")

        # DiffNote: extract file/line from first note's position
        first_note = notes[0]
        position = first_note.get("position") or {}
        file_path = position.get("new_path", "")
        line_num = position.get("new_line") or position.get("old_line") or ""

        context_parts = [
            f"MR !{mr_iid} (branch: {source_branch}) 收到 review 评论。",
            f"评论者: {author}",
        ]
        if file_path:
            context_parts.append(f"评论位置: {file_path}:{line_num}")
        context_parts.append(f"评论内容: {comment}")
        context_parts.append(f"discussion_id: {disc_id}")
        context_parts.append(
            "\n请处理这条 review 评论：\n"
            "1. 先 git fetch origin && git checkout 对应分支 && git pull，确保代码是最新的\n"
            "2. 读取相关代码，判断评论指出的问题是否确实存在\n"
            "3. 如果不存在问题，用 gitlab_reply_discussion 回复解释理由\n"
            "4. 如果存在问题，修复代码、git commit & push，"
            "然后用 gitlab_reply_discussion 回复修复方案\n"
            "5. 回复后用 gitlab_resolve_discussion 标记为已解决"
        )
        text = "\n".join(context_parts)

        chat_id = f"gitlab-mr-{mr_iid}"
        log.info("[POLL] Processing MR !%s disc %s by %s", mr_iid, disc_id, author)
        reply = self.handle_message(chat_id, f"gitlab:{author}", text)
        log.info("[POLL] MR !%s disc %s done: %s", mr_iid, disc_id, reply[:200])

        # Feishu notification
        if feishu_sender and self._config.feishu_chat_id:
            feishu_sender.send_text(
                self._config.feishu_chat_id,
                f"已自动处理 MR !{mr_iid} 的 review 评论 (by {author})\n{reply[:200]}",
            )

    def _try_acquire_poll_lock(self, key: str) -> bool:
        with self._lock:
            if not hasattr(self, "_poll_processing"):
                self._poll_processing: set[str] = set()
            if key in self._poll_processing:
                return False
            self._poll_processing.add(key)
            return True

    def _release_poll_lock(self, key: str):
        with self._lock:
            self._poll_processing.discard(key)

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

        self._sanitize_messages(sess.messages)
        sess.messages.append({"role": "user", "content": text})
        sess.ts = time.time()

        while True:
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

    @staticmethod
    def _sanitize_messages(messages: list[dict]):
        """Fix orphaned tool_use/tool_result blocks.

        Handles two cases:
        1. tool_result without preceding tool_use (from compression)
        2. tool_use without following tool_result (from mid-loop crash)

        Mutates the list in place.
        """
        # Pass 1: collect all tool_use ids and all tool_result ids
        tool_use_ids: set[str] = set()
        tool_result_ids: set[str] = set()
        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    tool_use_ids.add(block["id"])
                elif block.get("type") == "tool_result":
                    tool_result_ids.add(block.get("tool_use_id", ""))

        # Pass 2: remove orphaned tool_results and orphaned tool_uses
        i = 0
        removed = 0
        while i < len(messages):
            content = messages[i].get("content")
            if isinstance(content, list):
                cleaned = []
                for b in content:
                    if not isinstance(b, dict):
                        cleaned.append(b)
                        continue
                    # Remove tool_result without matching tool_use
                    if (b.get("type") == "tool_result"
                            and b.get("tool_use_id") not in tool_use_ids):
                        continue
                    # Remove tool_use without matching tool_result
                    if (b.get("type") == "tool_use"
                            and b.get("id") not in tool_result_ids):
                        continue
                    cleaned.append(b)

                if not cleaned:
                    messages.pop(i)
                    removed += 1
                    continue
                if len(cleaned) != len(content):
                    messages[i]["content"] = cleaned
            i += 1

        if removed:
            log.info("Sanitized messages: removed %d orphaned messages", removed)

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
