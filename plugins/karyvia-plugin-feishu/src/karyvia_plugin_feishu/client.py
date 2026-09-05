"""**SDK 出口①**：飞书的 HTTP API（开发方案 `D34`）。

职责：`Messenger` / `Cards` / `Reactions` / `Resources` 四个 Protocol 的生产实现——
发消息、回复、CardKit 四调用、反应增删、取 bot open_id、取父消息正文。
不负责：WS 连接（`gateway.py`）、任何判定（`normalize.py` / `outbound.py` / `stream.py`）。

**`lark_oapi` 只允许出现在本模块与 `gateway.py`**（有一条 AST 用例钉住）。两个出口而不是
一个，是因为飞书的 SDK 有两个形状完全不同的面：这里是 `requests` 的**同步阻塞**调用，
那边是 asyncio + 模块全局 loop。塞进一个文件是 650+ 行且混两种失败域。

**每一个调用都包一层 `asyncio.to_thread`**：`lark.Client` 的每个方法都会阻塞事件循环，
而一次 turn 里可能有几十次更新。把 `to_thread` 集中在这一个文件里，其余模块看到的是
普通的 `async def`。

**全部方法返回 `None` / `False` 而不是抛异常**：流式的每一步都有回落路径（见 `stream.py`
的状态机），用异常表达会让那个状态机变成一棵 try/except 树。**唯一例外是 `connect` 期间
的凭据错误**，那个在 `gateway.py` 里折成 `EXTERNAL_CHANNEL`。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Final

from karyvia.contracts import JsonValue, SecretStr

from .outbound import OutboundBody
from .stream import STREAM_ELEMENT_ID

__all__ = ["FeishuClient", "receive_id_type_for"]

#: 群聊 chat_id 的前缀。飞书的 `receive_id_type` 要按 id 的形状选。
_CHAT_ID_PREFIX: Final = "oc_"

#: 流式卡片的初始体（schema 2.0）。`streaming_mode` 必须在建卡时就打开——建完再开会多
#: 一次 settings 调用，也就多一次可能失败的地方。
_STREAM_CARD: Final[dict[str, JsonValue]] = {
    "schema": "2.0",
    "config": {"wide_screen_mode": True, "update_multi": True, "streaming_mode": True},
    "body": {"elements": [{"tag": "markdown", "content": "", "element_id": STREAM_ELEMENT_ID}]},
}


def receive_id_type_for(chat_id: str) -> str:
    """按 id 的形状选 `receive_id_type`。

    主路径恒是 `chat_id`——p2p 事件本来也带 `chat_id`，统一之后寻址只有一条路。
    这里的前缀判定是**防御性回落**：拿到一个不像 `oc_` 的 id 时按 `open_id` 发，
    总比直接 400 好。
    """
    return "chat_id" if chat_id.startswith(_CHAT_ID_PREFIX) else "open_id"


@dataclass(slots=True)
class FeishuClient:
    """四个 Protocol 的生产实现。`gateway.py` 建好 SDK client 之后交给它。"""

    # boundary: lark.Client，本模块是它仅有的两个出口之一
    raw: Any
    #: 诊断用的回调；失败只记不抛（见模块 docstring）。
    # boundary: 调用方自带的回调，签名不由本插件决定
    on_failure: Any = None

    # ------------------------------------------------------------------ Messenger

    async def send(self, chat_id: str, body: OutboundBody) -> str | None:
        """发一条消息。返回 `message_id`；失败返回 `None`。"""
        return await asyncio.to_thread(self._send_sync, chat_id, body)

    async def reply(self, message_id: str, body: OutboundBody, *, in_thread: bool) -> str | None:
        """回复一条消息。

        **`in_thread=True` 会让飞书新建一个话题**，因此只有配置显式开了
        `reply_to_message` 时才允许——判定在 `channel.py`，这里只忠实转发。
        """
        return await asyncio.to_thread(self._reply_sync, message_id, body, in_thread)

    # ------------------------------------------------------------------ Cards

    async def create(self) -> str | None:
        """建一张流式卡片，返回 `card_id`。"""
        return await asyncio.to_thread(self._create_card_sync)

    async def update(self, card_id: str, content: str, sequence: int) -> bool:
        return await asyncio.to_thread(self._update_card_sync, card_id, content, sequence)

    async def set_streaming(self, card_id: str, enabled: bool, sequence: int) -> bool:
        return await asyncio.to_thread(self._set_streaming_sync, card_id, enabled, sequence)

    # ------------------------------------------------------------------ Reactions

    async def add_reaction(self, message_id: str, emoji: str) -> str | None:
        return await asyncio.to_thread(self._add_reaction_sync, message_id, emoji)

    async def remove_reaction(self, message_id: str, reaction_id: str) -> None:
        await asyncio.to_thread(self._remove_reaction_sync, message_id, reaction_id)

    # ------------------------------------------------------------------ Resources

    async def bot_open_id(self) -> str:
        """取 bot 自己的 open_id。**失败返回空串而不是抛**——群聊 @ 门控有兜底启发式
        （见 `mentions.py`），拿不到身份不该让整条 Channel 起不来。"""
        return await asyncio.to_thread(self._bot_open_id_sync)

    async def message_text(self, message_id: str) -> str:
        """取一条消息的正文（用于回复上下文）。失败返回空串。"""
        return await asyncio.to_thread(self._message_text_sync, message_id)

    # ------------------------------------------------------------------ 同步实现

    def _fail(self, where: str, exc: Exception) -> None:
        """记一次失败。**只放类型名不放异常消息**——SDK 的异常文本可能带凭据
        （`D13` 的先例）。"""
        if self.on_failure is not None:
            self.on_failure(where, type(exc).__name__)

    def _send_sync(self, chat_id: str, body: OutboundBody) -> str | None:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        try:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type_for(chat_id))
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type(body.msg_type)
                    .content(body.content)
                    .build()
                )
                .build()
            )
            response = self.raw.im.v1.message.create(request)
        except Exception as exc:  # noqa: BLE001 - SDK 的原生异常不得逸出
            self._fail("send", exc)
            return None
        return _message_id_of(response)

    def _reply_sync(self, message_id: str, body: OutboundBody, in_thread: bool) -> str | None:
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        try:
            builder = (
                ReplyMessageRequestBody.builder()
                .msg_type(body.msg_type)
                .content(body.content)
                .reply_in_thread(in_thread)
            )
            request = (
                ReplyMessageRequest.builder()
                .message_id(message_id)
                .request_body(builder.build())
                .build()
            )
            response = self.raw.im.v1.message.reply(request)
        except Exception as exc:  # noqa: BLE001
            self._fail("reply", exc)
            return None
        return _message_id_of(response)

    def _create_card_sync(self) -> str | None:
        from lark_oapi.api.cardkit.v1 import CreateCardRequest, CreateCardRequestBody

        try:
            request = (
                CreateCardRequest.builder()
                .request_body(
                    CreateCardRequestBody.builder()
                    .type("card_json")
                    .data(json.dumps(_STREAM_CARD, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            response = self.raw.cardkit.v1.card.create(request)
        except Exception as exc:  # noqa: BLE001
            self._fail("card.create", exc)
            return None
        if not _ok(response):
            return None
        card_id = getattr(getattr(response, "data", None), "card_id", None)
        return card_id if isinstance(card_id, str) and card_id else None

    def _update_card_sync(self, card_id: str, content: str, sequence: int) -> bool:
        from lark_oapi.api.cardkit.v1 import (
            ContentCardElementRequest,
            ContentCardElementRequestBody,
        )

        try:
            request = (
                ContentCardElementRequest.builder()
                .card_id(card_id)
                .element_id(STREAM_ELEMENT_ID)
                .request_body(
                    ContentCardElementRequestBody.builder()
                    .content(content)
                    .sequence(sequence)
                    .build()
                )
                .build()
            )
            return _ok(self.raw.cardkit.v1.card_element.content(request))
        except Exception as exc:  # noqa: BLE001
            self._fail("card.update", exc)
            return False

    def _set_streaming_sync(self, card_id: str, enabled: bool, sequence: int) -> bool:
        import uuid

        from lark_oapi.api.cardkit.v1 import SettingsCardRequest, SettingsCardRequestBody

        try:
            settings = json.dumps({"config": {"streaming_mode": enabled}}, ensure_ascii=False)
            request = (
                SettingsCardRequest.builder()
                .card_id(card_id)
                .request_body(
                    SettingsCardRequestBody.builder()
                    .settings(settings)
                    .sequence(sequence)
                    # 每次换一个 uuid：飞书按它去重，复用会让第二次调用被静默忽略。
                    .uuid(uuid.uuid4().hex)
                    .build()
                )
                .build()
            )
            return _ok(self.raw.cardkit.v1.card.settings(request))
        except Exception as exc:  # noqa: BLE001
            self._fail("card.settings", exc)
            return False

    def _add_reaction_sync(self, message_id: str, emoji: str) -> str | None:
        from lark_oapi.api.im.v1 import (
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
            Emoji,
        )

        try:
            request = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji).build())
                    .build()
                )
                .build()
            )
            response = self.raw.im.v1.message_reaction.create(request)
        except Exception as exc:  # noqa: BLE001
            self._fail("reaction.add", exc)
            return None
        if not _ok(response):
            return None
        reaction_id = getattr(getattr(response, "data", None), "reaction_id", None)
        return reaction_id if isinstance(reaction_id, str) and reaction_id else None

    def _remove_reaction_sync(self, message_id: str, reaction_id: str) -> None:
        from lark_oapi.api.im.v1 import DeleteMessageReactionRequest

        try:
            request = (
                DeleteMessageReactionRequest.builder()
                .message_id(message_id)
                .reaction_id(reaction_id)
                .build()
            )
            self.raw.im.v1.message_reaction.delete(request)
        except Exception as exc:  # noqa: BLE001
            self._fail("reaction.remove", exc)

    def _bot_open_id_sync(self) -> str:
        from lark_oapi.core import JSON
        from lark_oapi.core.http import Transport
        from lark_oapi.core.model import BaseRequest, RequestOption

        try:
            request = (
                BaseRequest.builder()
                .http_method("GET")
                .uri("/open-apis/bot/v3/info")
                .token_types({_tenant_token_type()})
                .build()
            )
            response = Transport.execute(self.raw.config, request, RequestOption.builder().build())
            payload = JSON.unmarshal(str(response.raw.content, "utf-8"), dict)
        except Exception as exc:  # noqa: BLE001
            self._fail("bot.info", exc)
            return ""
        bot = payload.get("bot") if isinstance(payload, dict) else None
        open_id = bot.get("open_id") if isinstance(bot, dict) else None
        return open_id if isinstance(open_id, str) else ""

    def _message_text_sync(self, message_id: str) -> str:
        from lark_oapi.api.im.v1 import GetMessageRequest

        try:
            request = GetMessageRequest.builder().message_id(message_id).build()
            response = self.raw.im.v1.message.get(request)
        except Exception as exc:  # noqa: BLE001
            self._fail("message.get", exc)
            return ""
        if not _ok(response):
            return ""
        items = getattr(getattr(response, "data", None), "items", None) or []
        for item in items:
            body = getattr(item, "body", None)
            content = getattr(body, "content", None)
            if isinstance(content, str) and content:
                return content
        return ""


def _tenant_token_type() -> Any:  # boundary: lark 的枚举
    from lark_oapi.core.enum import AccessTokenType

    return AccessTokenType.TENANT


def _ok(response: Any) -> bool:  # boundary: lark 的响应对象
    """SDK 的响应统一有 `success()`。拿不到就当失败——沉默的成功比失败更糟。"""
    checker = getattr(response, "success", None)
    return bool(checker()) if callable(checker) else False


def _message_id_of(response: Any) -> str | None:  # boundary: lark 的响应对象
    if not _ok(response):
        return None
    message_id = getattr(getattr(response, "data", None), "message_id", None)
    return message_id if isinstance(message_id, str) and message_id else None


def credential_pair(app_id: SecretStr, app_secret: SecretStr) -> tuple[str, str]:
    """把一对凭据取成明文。**明文只在这里与 `gateway.build_client()` 之间流动一次。**"""
    return app_id.reveal(), app_secret.reveal()
