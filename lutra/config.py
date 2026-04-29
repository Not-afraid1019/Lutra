"""Lutra configuration — loaded from environment / .env file."""

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class LutraConfig(BaseSettings):
    # Claude API
    claude_api_key: str = ""
    claude_base_url: str = ""
    claude_model: str = "claude-opus-4-6"
    claude_max_tokens: int = 4096

    # Project
    project_dir: str = Field(
        default="",
        validation_alias=AliasChoices(
            "project_dir", "osbot_project_dir",  # legacy compat
        ),
    )
    bot_name: str = Field(
        default="Lutra",
        validation_alias=AliasChoices(
            "bot_name", "feishu_bot_name",  # legacy compat
        ),
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
    db_path: Path = Path("data/lutra.db")

    # Memory
    memory_inject_limit: int = 5

    # Feishu
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_chat_id: str = ""

    # JIRA
    jira_server: str = ""
    jira_pat: str = ""
    jira_aegis_cas: str = ""

    # Mimo (sensitive filter)
    mimo_api_key: str = ""
    mimo_base_url: str = "http://model.mify.ai.srv/v1"
    mimo_model: str = "mimo-v2-flash"
    mimo_provider_id: str = "xiaomi"

    # GitLab
    gitlab_url: str = ""              # e.g. "https://git.n.xiaomi.com"
    gitlab_pat: str = ""              # Personal Access Token (PRIVATE-TOKEN)
    gitlab_project: str = ""          # e.g. "ai-framework/osbot" (留空自动检测)
    gitlab_poll_interval: int = 0     # 轮询间隔(秒)，0=不启用间隔轮询
    gitlab_poll_cron: str = ""        # 固定时间，如 "09:00,14:00"，空=不启用
    gitlab_bot_username: str = ""     # bot 的 GitLab 用户名，用于跳过自身评论

    model_config = {"env_file": ".env", "extra": "ignore"}
