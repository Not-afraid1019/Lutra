#!/usr/bin/env python3
"""agent.py -- Lutra 单进程入口

编码助手：通过飞书接收指令，用工具读写代码、执行命令。

    飞书用户 ←WebSocket→ [lutra.feishu] ←直接调用→ [lutra.session]
    调试/外部 ←HTTP /api/chat→ [lutra.session]

用法:
    source .env
    python agent.py
    python agent.py --port 8901
"""

import argparse
import json
import logging
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from lutra.config import LutraConfig
from lutra.feishu import FeishuSender, start_ws
from lutra.llm import LLMClient
from lutra.memory.store import MemoryStore
from lutra.session import SessionManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("agent")
logging.getLogger("httpx").setLevel(logging.WARNING)

# Globals — initialized in main()
config: LutraConfig = None  # type: ignore[assignment]
session_mgr: SessionManager = None  # type: ignore[assignment]
feishu_sender: FeishuSender | None = None


# ======================================================================
# HTTP API
# ======================================================================


class APIHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/api/status":
            self._respond(200, {"status": "running", "model": config.claude_model})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/chat":
            self._handle_chat()
        elif self.path == "/api/send":
            self._handle_send()
        elif self.path == "/api/jira-token":
            self._handle_jira_token()
        else:
            self._respond(404, {"error": "not found"})

    def _handle_chat(self):
        body = self._read_body()
        chat_id = body.get("chat_id", "")
        text = body.get("text", "")

        if not chat_id or not text:
            self._respond(400, {"error": "chat_id and text required"})
            return

        sender_id = body.get("sender_id", "?")
        log.info("[HTTP] chat=%s sender=%s text=%s", chat_id, sender_id, text)

        reply = session_mgr.handle_message(chat_id, sender_id, text)
        self._respond(200, {"reply": reply})

    def _handle_send(self):
        if not feishu_sender:
            self._respond(503, {"error": "feishu not configured"})
            return

        body = self._read_body()
        chat_id = body.get("chat_id", "")
        content = body.get("content", "")

        if not chat_id or not content:
            self._respond(400, {"error": "chat_id and content required"})
            return

        feishu_sender.send_text(chat_id, str(content))
        self._respond(200, {"ok": True})

    def _handle_jira_token(self):
        body = self._read_body()
        token = body.get("token", "")
        if not token:
            self._respond(400, {"error": "token required"})
            return

        ok = session_mgr.update_jira_token(token)
        if ok:
            log.info("[HTTP] JIRA aegis_cas updated (len=%d)", len(token))
            self._respond(200, {"ok": True, "message": "JIRA token updated"})
        else:
            self._respond(503, {"error": "JIRA not configured"})

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def _respond(self, code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.debug("HTTP %s", fmt % args)


# ======================================================================
# Main
# ======================================================================


def main():
    global config, session_mgr, feishu_sender

    parser = argparse.ArgumentParser(description="Lutra Agent")
    parser.add_argument(
        "--port", type=int, default=8901, help="HTTP API 端口 (默认 8901)"
    )
    args = parser.parse_args()

    # ── Bootstrap ──
    config = LutraConfig()

    if not config.claude_api_key:
        print("ERROR: 请设置 CLAUDE_API_KEY")
        sys.exit(1)

    llm = LLMClient(config)
    store = MemoryStore(config.db_path)
    session_mgr = SessionManager(config, store, llm)

    # ── Feishu (optional) ──
    if config.feishu_app_id and config.feishu_app_secret:
        feishu_sender = FeishuSender(config.feishu_app_id, config.feishu_app_secret)
        start_ws(
            config.feishu_app_id,
            config.feishu_app_secret,
            feishu_sender,
            on_message=session_mgr.handle_message,
            chat_id_filter=config.feishu_chat_id,
        )
    else:
        log.warning("Feishu credentials not set, Feishu disabled")

    # ── Background cleanup ──
    def cleanup_loop():
        while True:
            time.sleep(300)
            session_mgr.cleanup_expired()

    threading.Thread(target=cleanup_loop, daemon=True).start()

    # ── HTTP server ──
    server = HTTPServer(("0.0.0.0", args.port), APIHandler)

    def shutdown(signum, frame):
        log.info("Shutting down…")
        session_mgr.save_all_sessions()
        store.close()
        server.shutdown()

    signal.signal(signal.SIGTERM, shutdown)

    from pathlib import Path
    work_dir = config.project_dir or str(Path.home())
    feishu_status = f"chat_id={config.feishu_chat_id or 'any'}" if feishu_sender else "disabled"
    print(f"\n  {config.bot_name} started")
    print(f"  Model       : {config.claude_model}")
    print(f"  Work dir    : {work_dir}")
    print(f"  HTTP API    : http://0.0.0.0:{args.port}")
    print(f"  Feishu      : {feishu_status}")
    print(f"  Commands    : /reset /recall")
    print(f"  Press Ctrl+C to stop\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        session_mgr.save_all_sessions()
        store.close()
        print(f"\n  {config.bot_name} stopped")


if __name__ == "__main__":
    main()
