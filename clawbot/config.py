"""ClawBot configuration — loaded from environment / .env file."""

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class ClawBotConfig(BaseSettings):
    # Claude API
    claude_api_key: str = ""
    claude_base_url: str = ""
    claude_model: str = "claude-opus-4-6"
    claude_max_tokens: int = 4096

    # Project
    project_dir: str = Field(
        default="",
        validation_alias=AliasChoices("project_dir", "osbot_project_dir"),
    )
    bot_name: str = Field(
        default="ClawBot",
        validation_alias=AliasChoices("bot_name", "feishu_bot_name"),
    )

    # Agent loop
    max_tool_rounds: int = 30

    # Context management
    context_threshold: int = 150_000
    context_keep_recent: int = 30

    # Session
    session_ttl: int = 3600
    session_save_interval: int = 60
    session_save_messages: int = 5

    # Paths
    db_path: Path = Path("data/clawbot.db")

    # Memory
    memory_inject_limit: int = 5

    # Feishu
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_chat_id: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}
