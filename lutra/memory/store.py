"""SQLite persistence layer — memories, sessions, characters."""

import json
import logging
import sqlite3
import threading
from pathlib import Path

log = logging.getLogger("lutra.memory.store")


class MemoryStore:
    def __init__(self, db_path: Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._init_tables()
        log.info("Database ready: %s", db_path)

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_tables(self):
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id            TEXT PRIMARY KEY,
                    chat_id       TEXT NOT NULL,
                    character_id  TEXT DEFAULT '',
                    memory_type   TEXT NOT NULL,
                    content       TEXT NOT NULL,
                    keywords      TEXT DEFAULT '',
                    importance    REAL DEFAULT 1.0,
                    message_count INTEGER DEFAULT 0,
                    created_at    TEXT DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_memories_chat
                    ON memories(chat_id, character_id);
                CREATE INDEX IF NOT EXISTS idx_memories_type
                    ON memories(memory_type);
                CREATE INDEX IF NOT EXISTS idx_memories_keywords
                    ON memories(keywords);

                CREATE TABLE IF NOT EXISTS sessions (
                    chat_id    TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS characters (
                    id            TEXT PRIMARY KEY,
                    name          TEXT NOT NULL,
                    system_prompt TEXT NOT NULL,
                    creator_id    TEXT DEFAULT '',
                    created_at    TEXT DEFAULT (datetime('now')),
                    metadata_json TEXT DEFAULT '{}'
                );
                """
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Memory CRUD
    # ------------------------------------------------------------------

    def save_memory(self, memory) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO memories
                   (id, chat_id, character_id, memory_type, content,
                    keywords, importance, message_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    memory.id,
                    memory.chat_id,
                    memory.character_id,
                    memory.memory_type,
                    memory.content,
                    memory.keywords,
                    memory.importance,
                    memory.message_count,
                    memory.created_at.isoformat(),
                ),
            )
            self._conn.commit()

    def get_memories(
        self,
        chat_id: str | None = None,
        character_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 10,
    ) -> list[sqlite3.Row]:
        clauses, params = ["1=1"], []
        if chat_id:
            clauses.append("chat_id = ?")
            params.append(chat_id)
        if character_id:
            clauses.append("character_id = ?")
            params.append(character_id)
        if memory_type:
            clauses.append("memory_type = ?")
            params.append(memory_type)
        query = (
            f"SELECT * FROM memories WHERE {' AND '.join(clauses)} "
            f"ORDER BY importance DESC, created_at DESC LIMIT ?"
        )
        params.append(limit)
        return self._conn.execute(query, params).fetchall()

    def search_by_keyword(
        self,
        keyword: str,
        character_id: str | None = None,
        limit: int = 5,
    ) -> list[sqlite3.Row]:
        clauses = ["keywords LIKE ?"]
        params: list = [f"%{keyword}%"]
        if character_id:
            clauses.append("character_id = ?")
            params.append(character_id)
        query = (
            f"SELECT * FROM memories WHERE {' AND '.join(clauses)} "
            f"ORDER BY importance DESC, created_at DESC LIMIT ?"
        )
        params.append(limit)
        return self._conn.execute(query, params).fetchall()

    # ------------------------------------------------------------------
    # Session CRUD
    # ------------------------------------------------------------------

    def save_session(self, chat_id: str, state_json: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO sessions (chat_id, state_json, updated_at)
                   VALUES (?, ?, datetime('now'))""",
                (chat_id, state_json),
            )
            self._conn.commit()

    def load_session(self, chat_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT state_json FROM sessions WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return row["state_json"] if row else None

    def load_all_sessions(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT chat_id, state_json FROM sessions"
        ).fetchall()

    def delete_session(self, chat_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM sessions WHERE chat_id = ?", (chat_id,)
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Character CRUD
    # ------------------------------------------------------------------

    def save_character(self, character) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO characters
                   (id, name, system_prompt, creator_id, created_at, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    character.id,
                    character.name,
                    character.system_prompt,
                    character.creator_id,
                    character.created_at.isoformat(),
                    json.dumps(character.metadata, ensure_ascii=False),
                ),
            )
            self._conn.commit()

    def get_character(
        self,
        character_id: str | None = None,
        name: str | None = None,
    ) -> sqlite3.Row | None:
        if character_id:
            return self._conn.execute(
                "SELECT * FROM characters WHERE id = ?", (character_id,)
            ).fetchone()
        if name:
            return self._conn.execute(
                "SELECT * FROM characters WHERE name = ?", (name,)
            ).fetchone()
        return None

    def list_characters(self, creator_id: str | None = None) -> list[sqlite3.Row]:
        if creator_id:
            return self._conn.execute(
                "SELECT * FROM characters WHERE creator_id = ? ORDER BY created_at DESC",
                (creator_id,),
            ).fetchall()
        return self._conn.execute(
            "SELECT * FROM characters ORDER BY created_at DESC"
        ).fetchall()

    # ------------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()
        log.info("Database connection closed")
