"""Feishu I/O — WebSocket receiver + message sender.

No business logic here. Receives messages from Feishu, calls a callback,
and sends replies back.
"""

import json
import logging
import re
import threading
import time
from typing import Callable

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    DeleteMessageReactionRequest,
    P2ImMessageReceiveV1,
)
from lark_oapi.api.im.v1.model import Emoji

log = logging.getLogger("lutra.feishu")

_AT_PATTERN = re.compile(r"@_user_\d+\s*")

TYPING_EMOJI = "Typing"  # Feishu reaction emoji_type


# ======================================================================
# Sender
# ======================================================================


class FeishuSender:
    """Send messages and manage reactions in Feishu chats."""

    def __init__(self, app_id: str, app_secret: str):
        self._client = (
            lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        )

    # ── Messages ──

    def send_text(self, chat_id: str, text: str) -> str | None:
        """Send a text message. Returns message_id or None."""
        return self._send(chat_id, "text", json.dumps({"text": text}))

    def send_card(self, chat_id: str, card: dict) -> str | None:
        return self._send(
            chat_id, "interactive", json.dumps(card, ensure_ascii=False)
        )

    def _send(self, chat_id: str, msg_type: str, content: str) -> str | None:
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type(msg_type)
            .content(content)
            .build()
        )
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(body)
            .build()
        )
        resp = self._client.im.v1.message.create(req)
        if not resp.success():
            log.error("[SEND] failed: %s %s", resp.code, resp.msg)
            return None
        return resp.data.message_id

    # ── Reactions ──

    def add_reaction(self, message_id: str, emoji_type: str = TYPING_EMOJI) -> str | None:
        """Add a reaction to a message. Returns reaction_id or None."""
        req = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(
                CreateMessageReactionRequestBody.builder()
                .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                .build()
            )
            .build()
        )
        resp = self._client.im.v1.message_reaction.create(req)
        if not resp.success():
            log.warning("[REACTION] add failed: %s %s", resp.code, resp.msg)
            return None
        return resp.data.reaction_id

    def remove_reaction(self, message_id: str, reaction_id: str) -> bool:
        """Remove a reaction from a message."""
        req = (
            DeleteMessageReactionRequest.builder()
            .message_id(message_id)
            .reaction_id(reaction_id)
            .build()
        )
        resp = self._client.im.v1.message_reaction.delete(req)
        if not resp.success():
            log.warning("[REACTION] remove failed: %s %s", resp.code, resp.msg)
            return False
        return True


# ======================================================================
# WebSocket receiver
# ======================================================================

_seen_msg_ids: dict[str, float] = {}
_seen_lock = threading.Lock()
_DEDUP_TTL = 60

# Updated to "now" (epoch ms str) before each WS connect/reconnect.
# Any message with create_time < this value is silently dropped.
_connect_cutoff_ms: list[str] = [str(int(time.time() * 1000))]


def _is_duplicate(message_id: str) -> bool:
    now = time.time()
    with _seen_lock:
        expired = [k for k, t in _seen_msg_ids.items() if now - t > _DEDUP_TTL]
        for k in expired:
            del _seen_msg_ids[k]
        if message_id in _seen_msg_ids:
            return True
        _seen_msg_ids[message_id] = now
        return False


def start_ws(
    app_id: str,
    app_secret: str,
    sender: FeishuSender,
    on_message: Callable[[str, str, str], str],
    chat_id_filter: str = "",
):
    """Start Feishu WebSocket listener in a daemon thread.

    Flow per message:
      1. Add reaction to user's message (typing indicator)
      2. Call on_message() to get reply
      3. Remove reaction
      4. Send reply

    Uses auto_reconnect=True (lark handles reconnect internally via its
    event loop). We patch Client._connect to update _connect_cutoff_ms
    before each connect/reconnect, so stale messages are always dropped.
    """

    def _process(msg, event) -> None:
        """Handle a single message — runs in a worker thread."""
        sender_id = (
            event.sender.sender_id.open_id if event.sender.sender_id else "?"
        )
        log.info(
            "[MSG] chat=%s type=%s sender=%s text=%s",
            msg.chat_id,
            msg.chat_type,
            sender_id,
            msg._text,
        )

        # 1. Typing indicator
        reaction_id = sender.add_reaction(msg.message_id)

        try:
            # 2. Process (may take minutes for Claude calls)
            reply = on_message(msg.chat_id, sender_id, msg._text)
        except Exception as e:
            log.error("[MSG] handler error: %s", e)
            reply = f"处理出错: {e}"
        finally:
            # 3. Remove typing indicator
            if reaction_id:
                sender.remove_reaction(msg.message_id, reaction_id)

        # 4. Send reply
        if reply:
            sender.send_text(msg.chat_id, reply)

    def handler(data: P2ImMessageReceiveV1) -> None:
        event = data.event
        msg = event.message

        if msg.message_type != "text":
            return
        if _is_duplicate(msg.message_id):
            return

        # Drop messages created before the current connection was established
        if msg.create_time and msg.create_time < _connect_cutoff_ms[0]:
            log.debug("[MSG] dropping pre-connect message %s (created %s < cutoff %s)",
                      msg.message_id, msg.create_time, _connect_cutoff_ms[0])
            return

        try:
            content = json.loads(msg.content)
            text = content.get("text", "")
        except (json.JSONDecodeError, TypeError):
            return

        if msg.chat_type == "group":
            if not msg.mentions:
                return
            if chat_id_filter and msg.chat_id != chat_id_filter:
                return

        text = _AT_PATTERN.sub("", text).strip()
        if not text:
            return

        # Stash parsed text on msg object for the worker thread
        msg._text = text

        # Dispatch to worker thread so we don't block the event loop
        # (blocking prevents WebSocket ping/pong → 3003 ping_timeout)
        threading.Thread(
            target=_process, args=(msg, event), daemon=True
        ).start()

    _noop = lambda data: None  # noqa: E731
    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(handler)
        .register_p2_im_message_reaction_created_v1(_noop)
        .register_p2_im_message_reaction_deleted_v1(_noop)
        .register_p2_im_message_message_read_v1(_noop)
        .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(_noop)
        .build()
    )
    ws_client = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
        auto_reconnect=True,
    )

    # Patch _connect so cutoff is updated on every connect AND reconnect.
    # lark_oapi calls _connect() both on initial start and after each
    # disconnect/reconnect, so this covers all cases.
    _original_connect = ws_client._connect

    async def _connect_with_cutoff():
        _connect_cutoff_ms[0] = str(int(time.time() * 1000))
        log.info("Feishu WebSocket connecting (cutoff=%s, filter=%s)",
                 _connect_cutoff_ms[0], chat_id_filter or "any")
        await _original_connect()

    ws_client._connect = _connect_with_cutoff

    threading.Thread(target=ws_client.start, daemon=True).start()
