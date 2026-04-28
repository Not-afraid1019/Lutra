"""Data models used across ClawBot."""

import time
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class SessionState(BaseModel):
    """Session state — messages stored as raw Anthropic API dicts."""

    chat_id: str
    messages: list[dict] = []  # raw API format (supports tool_use content)
    last_input_tokens: int = 0
    message_count_since_save: int = 0
    last_save_time: float = Field(default_factory=time.time)
    ts: float = Field(default_factory=time.time)


class Memory(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    chat_id: str
    character_id: str = ""  # kept for schema compat, unused
    memory_type: str  # session_summary | context_compression
    content: str
    keywords: str = ""
    importance: float = 1.0
    message_count: int = 0
    created_at: datetime = Field(default_factory=datetime.now)
