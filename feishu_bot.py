#!/usr/bin/env python3
"""feishu_bot.py -- 飞书消息转发器 (常驻进程)

纯 I/O 通道：接收飞书群消息，转发给 Agent；接收 Agent 推送，发到飞书群。

三进程架构:
    飞书群聊用户
        │ (WebSocket)
        ▼
    feishu_bot.py (:8900)  ──POST /api/chat──►  agent (:8901)
                           ◄──POST /api/send──

HTTP API:
    POST /api/send      Agent 推送消息到飞书  {"chat_id", "msg_type", "content"}
    GET  /api/status     健康检查

环境变量:
    FEISHU_APP_ID       飞书应用 App ID (必填)
    FEISHU_APP_SECRET   飞书应用 App Secret (必填)
    FEISHU_CHAT_ID      默认群聊 ID (可选，用于过滤群聊)
    FEISHU_BOT_NAME     机器人名称 (可选，默认 "ClawBot")

用法:
    export FEISHU_APP_ID=cli_xxx FEISHU_APP_SECRET=xxx
    python feishu_bot.py                                  # 启动常驻进程
    python feishu_bot.py --port 8900                      # 指定 API 端口
    python feishu_bot.py --agent-api http://127.0.0.1:8901
"""

import argparse
import json
import logging
import os
import re
import sys
import threading
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("feishu_bot")

logging.getLogger("httpx").setLevel(logging.WARNING)


# ======================================================================
# FeishuNotifier — 飞书消息发送 (从 otf 提取并精简)
# ======================================================================

class FeishuNotifier:
    """飞书消息发送器，支持文本、图片、卡片、报告。"""

    def __init__(self, app_id: str, app_secret: str,
                 bot_name: str = "ClawBot"):
        self.app_id = app_id
        self.app_secret = app_secret
        self.bot_name = bot_name
        self.client = lark.Client.builder() \
            .app_id(app_id) \
            .app_secret(app_secret) \
            .build()

    def send_text(self, chat_id: str, text: str) -> bool:
        content = json.dumps({"text": text})
        body = CreateMessageRequestBody.builder() \
            .receive_id(chat_id) \
            .msg_type("text") \
            .content(content) \
            .build()
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(body) \
            .build()

        resp = self.client.im.v1.message.create(req)
        if not resp.success():
            log.error("[feishu] text send failed: %s %s", resp.code, resp.msg)
            return False
        return True

    def upload_image(self, image_path: str) -> str:
        if not os.path.isfile(image_path):
            log.error("[feishu] image not found: %s", image_path)
            return ""

        with open(image_path, "rb") as f:
            body = CreateImageRequestBody.builder() \
                .image_type("message") \
                .image(f) \
                .build()
            req = CreateImageRequest.builder() \
                .request_body(body) \
                .build()

            resp = self.client.im.v1.image.create(req)
            if not resp.success():
                log.error("[feishu] image upload failed: %s %s",
                          resp.code, resp.msg)
                return ""
            return resp.data.image_key

    def send_image(self, chat_id: str, image_key: str) -> bool:
        content = json.dumps({"image_key": image_key})
        body = CreateMessageRequestBody.builder() \
            .receive_id(chat_id) \
            .msg_type("image") \
            .content(content) \
            .build()
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(body) \
            .build()

        resp = self.client.im.v1.message.create(req)
        if not resp.success():
            log.error("[feishu] image send failed: %s %s", resp.code, resp.msg)
            return False
        return True

    def send_report(self, chat_id: str, title: str,
                    summary_lines: list[str],
                    image_paths: list[str],
                    report_url: str = "",
                    at_all: bool = False) -> bool:
        image_keys = []
        for path in image_paths:
            key = self.upload_image(path)
            if key:
                image_keys.append(key)

        card = self._build_report_card(
            title, summary_lines, image_keys, report_url, at_all)
        return self._send_card(chat_id, card)

    def _send_card(self, chat_id: str, card: dict) -> bool:
        content = json.dumps(card, ensure_ascii=False)
        body = CreateMessageRequestBody.builder() \
            .receive_id(chat_id) \
            .msg_type("interactive") \
            .content(content) \
            .build()
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(body) \
            .build()

        resp = self.client.im.v1.message.create(req)
        if not resp.success():
            log.error("[feishu] card send failed: %s %s", resp.code, resp.msg)
            return False
        log.info("[feishu] card sent to %s", chat_id)
        return True

    @staticmethod
    def _build_report_card(title: str, summary_lines: list[str],
                           image_keys: list[str], report_url: str,
                           at_all: bool = False) -> dict:
        elements = []
        if at_all:
            elements.append({"tag": "markdown", "content": "<at id=all></at>"})
        if summary_lines:
            elements.append({
                "tag": "markdown", "content": "\n".join(summary_lines)})
            elements.append({"tag": "hr"})
        for key in image_keys:
            elements.append({
                "tag": "img", "img_key": key,
                "alt": {"tag": "plain_text", "content": "报告截图"},
            })
        if report_url:
            if image_keys:
                elements.append({"tag": "hr"})
            elements.append({
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "查看完整报告"},
                    "type": "primary",
                    "url": report_url,
                }],
            })
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": elements,
        }


# ======================================================================
# Helpers
# ======================================================================

_AT_PATTERN = re.compile(r"@_user_\d+\s*")

_seen_msg_ids: dict[str, float] = {}
_seen_lock = threading.Lock()
_DEDUP_TTL = 60


def _is_duplicate(message_id: str) -> bool:
    import time
    now = time.time()
    with _seen_lock:
        expired = [k for k, t in _seen_msg_ids.items() if now - t > _DEDUP_TTL]
        for k in expired:
            del _seen_msg_ids[k]
        if message_id in _seen_msg_ids:
            return True
        _seen_msg_ids[message_id] = now
        return False


def _strip_at_prefix(text: str) -> str:
    return _AT_PATTERN.sub("", text).strip()


# ======================================================================
# Agent 转发
# ======================================================================

_AGENT_API = ""


def _forward_to_agent(chat_id: str, sender_id: str, text: str) -> str:
    if not _AGENT_API:
        return "Agent 服务未配置"

    url = f"{_AGENT_API}/api/chat"
    body = json.dumps({
        "chat_id": chat_id,
        "sender_id": sender_id,
        "text": text,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data.get("reply", "")
    except Exception as e:
        log.error("[FORWARD] agent API failed: %s", e)
        return "Agent 服务暂时不可用，请稍后重试。"


# ======================================================================
# Feishu WebSocket handler
# ======================================================================

def _make_feishu_handler(notifier: FeishuNotifier, chat_id_filter: str):
    def on_message(data: P2ImMessageReceiveV1) -> None:
        event = data.event
        msg = event.message
        sender = event.sender

        if msg.message_type != "text":
            return

        if _is_duplicate(msg.message_id):
            log.debug("[DEDUP] skip duplicate message: %s", msg.message_id)
            return

        try:
            content = json.loads(msg.content)
            text = content.get("text", "")
        except (json.JSONDecodeError, TypeError):
            return

        chat_type = msg.chat_type

        if chat_type == "group":
            mentions = msg.mentions
            if not mentions:
                return
            if chat_id_filter and msg.chat_id != chat_id_filter:
                return

        text = _strip_at_prefix(text)
        if not text:
            return

        sender_id = sender.sender_id.open_id if sender.sender_id else "?"
        log.info("[MSG] chat=%s type=%s sender=%s text=%s",
                 msg.chat_id, chat_type, sender_id, text)

        reply = _forward_to_agent(msg.chat_id, sender_id, text)
        if reply:
            notifier.send_text(msg.chat_id, reply)

    return on_message


def start_feishu_ws(app_id: str, app_secret: str,
                    notifier: FeishuNotifier, chat_id_filter: str):
    handler_fn = _make_feishu_handler(notifier, chat_id_filter)
    _noop = lambda data: None  # noqa: E731
    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(handler_fn)
        .register_p2_im_message_reaction_created_v1(_noop)
        .register_p2_im_message_reaction_deleted_v1(_noop)
        .register_p2_im_message_read_v1(_noop)
        .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(_noop)
        .build()
    )
    cli = lark.ws.Client(
        app_id, app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
        auto_reconnect=False,
    )
    t = threading.Thread(target=cli.start, daemon=True)
    t.start()
    log.info("Feishu WebSocket started")


# ======================================================================
# HTTP API — 接收 Agent 推送
# ======================================================================

class BotAPIHandler(BaseHTTPRequestHandler):

    notifier: FeishuNotifier = None

    def do_GET(self):
        if self.path == "/api/status":
            self._respond(200, {"status": "running"})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/send":
            self._handle_send()
        else:
            self._respond(404, {"error": "not found"})

    def _handle_send(self):
        body = self._read_body()
        chat_id = body.get("chat_id", "")
        msg_type = body.get("msg_type", "text")
        content = body.get("content", "")

        if not chat_id or not content:
            self._respond(400, {"error": "chat_id and content required"})
            return

        try:
            if msg_type == "card" and isinstance(content, dict):
                self.notifier._send_card(chat_id, content)
            elif msg_type == "image":
                image_key = self.notifier.upload_image(str(content))
                if image_key:
                    self.notifier.send_image(chat_id, image_key)
                else:
                    log.error("[SEND] image upload failed: %s", content)
                    self._respond(500, {"error": "image upload failed"})
                    return
            elif msg_type == "report" and isinstance(content, dict):
                self.notifier.send_report(
                    chat_id,
                    content.get("title", ""),
                    content.get("summary", []),
                    content.get("images", []),
                    content.get("report_url", ""),
                    at_all=content.get("at_all", False),
                )
            else:
                self.notifier.send_text(chat_id, str(content))
            self._respond(200, {"ok": True})
        except Exception as e:
            log.error("[SEND] failed: %s", e)
            self._respond(500, {"error": str(e)})

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw)

    def _respond(self, code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        log.debug("HTTP %s", format % args)


# ======================================================================
# Main
# ======================================================================

def main():
    parser = argparse.ArgumentParser(
        description="飞书消息转发器 (纯 I/O 通道)")
    parser.add_argument("--chat-id", default=None, help="目标群聊 ID")
    parser.add_argument("--port", type=int, default=8900,
                        help="HTTP API 端口 (默认 8900)")
    parser.add_argument("--agent-api", default="http://127.0.0.1:8901",
                        help="Agent HTTP API 地址 (默认 http://127.0.0.1:8901)")
    args = parser.parse_args()

    # 从环境变量读取飞书配置
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")

    if not app_id or not app_secret:
        print("ERROR: 请设置环境变量 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        sys.exit(1)

    chat_id = args.chat_id or os.environ.get("FEISHU_CHAT_ID", "")
    bot_name = os.environ.get("FEISHU_BOT_NAME", "ClawBot")

    global _AGENT_API
    _AGENT_API = args.agent_api.rstrip("/")

    notifier = FeishuNotifier(app_id, app_secret, bot_name)

    # 启动飞书 WebSocket
    start_feishu_ws(app_id, app_secret, notifier, chat_id)

    # 启动 HTTP API (供 Agent 推送)
    BotAPIHandler.notifier = notifier
    server = HTTPServer(("0.0.0.0", args.port), BotAPIHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info("HTTP API started on 0.0.0.0:%d", args.port)

    print(f"\n  feishu_bot started (消息转发模式)")
    print(f"  Bot name       : {bot_name}")
    print(f"  Feishu chat_id : {chat_id or 'any'}")
    print(f"  HTTP API       : http://0.0.0.0:{args.port}")
    print(f"  Agent API      : {_AGENT_API}")
    print(f"  Press Ctrl+C to stop\n")

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\n  Bot stopped")


if __name__ == "__main__":
    main()
