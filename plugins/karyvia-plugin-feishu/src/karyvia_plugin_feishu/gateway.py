"""**SDK 出口②**：飞书的 WebSocket 长连接。

职责：惰性 import `lark_oapi`、把模块全局 loop 换成可追踪的代理、连接与干净停止、
事件注册、把 SDK 事件拍成本插件自己的 `RawInbound`。
不负责：判定该不该处理（`normalize.py`）、HTTP 调用（`client.py`）、任何渲染。

## 为什么要碰 SDK 的私有 API

`lark_oapi/ws/client.py` 在**模块顶层**跑 `asyncio.get_event_loop()` 存进模块全局 `loop`；
`Client.start()` 用 `run_until_complete` 把线程占死且**没有 `stop()`**；而 `_connect` 与
`_receive_message_loop` 都用**那个模块全局 loop** `create_task`。于是有两条硬约束：

1. 模块全局 `loop` 必须等于「正在跑的那个 loop」，否则 `create_task` 会往一个没在跑的
   loop 上排任务，消息永远收不到；
2. 想干净地停下来，只能自己驱动 `_connect` / `_disconnect` / `_ping_loop` 并自己管任务。

因此本模块碰四个私有属性：`_connect` / `_disconnect` / `_ping_loop` / `_auto_reconnect`。
`connect()` 里先 `hasattr` 检查一遍，缺任何一个就报 `EXTERNAL_CHANNEL` + 一句能照做的话，
而不是在半路抛 `AttributeError`。

## 与 legacy 的差别：去掉线程，把猴补换成 loop 代理

legacy 要跑 N 个实例，因此起了一个专用 daemon 线程 + 专用 loop，还猴补了实例上的
`_receive_message_loop` 来拿任务句柄，每条消息再 `run_coroutine_threadsafe` 跨回业务 loop。
**单实例之后这些全不需要**：直接在实例主 loop 上驱动，事件回调因此就在主 loop 上，
整条跨线程通道消失。

拿任务句柄改用 **`_TaskTracker`**——一个 `__getattr__` 委托真 loop、只覆写 `create_task`
的代理。它比猴补好在两处：**一个对象覆盖全部三处 `create_task`**（receive loop、
handle message、ping），且重连之后新建的 receive loop 也自动被追踪；而猴补依赖「patch 的是
实例属性所以能活过重连」这条隐性前提。

**重连交给 SDK 自己的 `_auto_reconnect`**（ 定的调子：重连是外部组件的策略，
我们不写第二套）。但**首次** `_connect()` 失败仍然折成 `EXTERNAL_CHANNEL` 抛出——
那通常是 app_id/secret 错，重连一万次也不会好。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from contextlib import suppress
from typing import Any, Final

from karyvia.contracts import ErrorCode, JsonValue, KaryviaError, SecretStr

from .mentions import Mention, mentions_from
from .normalize import RawInbound

__all__ = ["MISSING_SDK_FIX", "FeishuGateway", "event_to_raw"]

MISSING_SDK_FIX: Final = "karyvia plugins install <插件来源>（不要使用 --no-deps）"

_MISSING_SDK: Final = "飞书 Channel 需要 lark-oapi，但当前环境里没有装。"
_CONNECT_FAILED: Final = "无法连接飞书 WebSocket 长连接。"
_INCOMPATIBLE_SDK: Final = "当前 lark-oapi 版本缺少本插件需要的内部接口。"

#: 本模块依赖的四个 SDK 私有属性。见模块 docstring。
_REQUIRED_ATTRS: Final[tuple[str, ...]] = (
    "_connect",
    "_disconnect",
    "_ping_loop",
    "_auto_reconnect",
)

#: 这些事件注册上去只为消费掉它们——不注册的话 SDK 会为每一条打一句
#: 「processor not found」的噪声日志。`getattr` 探测是因为不同 SDK 版本有增减。
_QUIET_EVENTS: Final[tuple[str, ...]] = (
    "register_p2_im_message_reaction_created_v1",
    "register_p2_im_message_reaction_deleted_v1",
    "register_p2_im_message_message_read_v1",
    "register_p2_im_chat_access_event_bot_p2p_chat_entered_v1",
    "register_p2_im_chat_member_bot_added_v1",
    "register_p2_im_chat_member_bot_deleted_v1",
)


class _TaskTracker:
    """包住事件循环，只为记下 SDK 派出去的任务。

    `__getattr__` 把其余成员透传给真 loop，因此 SDK 将来用到别的 loop 方法也不会受影响
    ——**只有 `create_task` 被改写**。
    """

    __slots__ = ("_loop", "tasks")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.tasks: set[asyncio.Task[Any]] = set()  # boundary: SDK 派的任务返回类型不定

    def create_task(self, coro: Any, **kwargs: Any) -> Any:  # boundary: 转发给真 loop
        task = self._loop.create_task(coro, **kwargs)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def __getattr__(self, name: str) -> Any:  # boundary: 透传给真 loop
        return getattr(self._loop, name)


def _text_of(value: object) -> str:
    return value if isinstance(value, str) else ""


def event_to_raw(event: Any) -> RawInbound:  # boundary: lark 的事件对象，只在本模块解构
    """`P2ImMessageReceiveV1` → `RawInbound`。**这是 SDK 对象能走到的最后一步。**

    纯函数（不碰网络），因此用例可以用一个手写的假事件驱动它——那正是 `tests/_fakes.py`
    在做的事，也是「不装 `lark-oapi` 也能跑绝大多数用例」的支点。
    """
    body = getattr(event, "event", None)
    message = getattr(body, "message", None)
    sender = getattr(body, "sender", None)
    sender_id = getattr(sender, "sender_id", None)
    raw_mentions = getattr(message, "mentions", None) or ()
    mentions: tuple[Mention, ...] = mentions_from(
        [
            {
                "key": _text_of(getattr(item, "key", "")),
                "name": _text_of(getattr(item, "name", "")),
                "id": {
                    "open_id": _text_of(getattr(getattr(item, "id", None), "open_id", "")),
                    "user_id": _text_of(getattr(getattr(item, "id", None), "user_id", "")),
                },
            }
            for item in raw_mentions
        ]
    )
    return RawInbound(
        message_id=_text_of(getattr(message, "message_id", "")),
        chat_id=_text_of(getattr(message, "chat_id", "")),
        chat_type=_text_of(getattr(message, "chat_type", "")),
        msg_type=_text_of(getattr(message, "message_type", "")),
        sender_id=_text_of(getattr(sender_id, "open_id", "")),
        sender_type=_text_of(getattr(sender, "sender_type", "")),
        content=_text_of(getattr(message, "content", "")),
        create_time=_text_of(getattr(message, "create_time", "")),
        root_id=_text_of(getattr(message, "root_id", "")) or None,
        parent_id=_text_of(getattr(message, "parent_id", "")) or None,
        thread_id=_text_of(getattr(message, "thread_id", "")) or None,
        mentions=mentions,
    )


class FeishuGateway:
    """WS 连接的全部生命周期。`channel.py` 持有一个。"""

    __slots__ = ("_client", "_domain", "_http", "_on_event", "_ping", "_secret", "_tracker", "_app")

    def __init__(
        self,
        *,
        app_id: SecretStr,
        app_secret: SecretStr,
        domain: str,
        on_event: Callable[[Any], Coroutine[Any, Any, None]],  # boundary: lark 的事件对象
    ) -> None:
        self._app = app_id
        self._secret = app_secret
        self._domain = domain
        self._on_event = on_event
        self._client: Any = None  # boundary: lark.ws.Client
        self._http: Any = None  # boundary: lark.Client
        self._tracker: _TaskTracker | None = None
        self._ping: asyncio.Task[None] | None = None

    @property
    def http(self) -> Any:  # boundary: lark.Client，交给 `client.FeishuClient`
        return self._http

    async def connect(self) -> None:
        """起 WS 长连接。

        **异常约定**：SDK 没装 / 版本不兼容 / 连接失败都抛 `EXTERNAL_CHANNEL`，三者的
        `detail` 分得开——补救动作分别是装依赖、锁版本、查凭据与网络。
        """
        lark, ws_module, domain = await asyncio.to_thread(_import_lark, self._domain)
        app_id, app_secret = self._app.reveal(), self._secret.reveal()
        self._http = lark.Client.builder().app_id(app_id).app_secret(app_secret).domain(domain).build()
        client = lark.ws.Client(
            app_id, app_secret, domain=domain, event_handler=self._handler(lark)
        )
        missing = [name for name in _REQUIRED_ATTRS if not hasattr(client, name)]
        if missing:
            raise KaryviaError(
                ErrorCode.EXTERNAL_CHANNEL,
                _INCOMPATIBLE_SDK,
                detail={"missing": missing, "fix": "锁定 lark-oapi>=1.5,<2"},
            )
        # **必须在这里赋、而且赋的是正在跑的那个 loop**：SDK 用模块全局 loop 派任务。
        tracker = _TaskTracker(asyncio.get_running_loop())
        ws_module.loop = tracker
        self._tracker = tracker
        self._client = client
        try:
            await client._connect()  # noqa: SLF001 - 见模块 docstring
        except Exception as exc:  # noqa: BLE001 - SDK 的原生异常不得逸出
            await self.close()
            raise KaryviaError(
                ErrorCode.EXTERNAL_CHANNEL,
                _CONNECT_FAILED,
                detail={"exception": type(exc).__name__},
                retryable=True,
            ) from exc
        self._ping = asyncio.create_task(client._ping_loop(), name="feishu:ping")  # noqa: SLF001

    async def close(self) -> None:
        """断开。**约定不抛**（`Channel.stop()` 的契约），多次调用安全。

        **以下五步的顺序不可交换**：
        不先取消 `recv()` 就关 socket，SDK 会把 close code 1000 记成 error，
        `_receive_message_loop` 的 except 分支随即触发一次**不该有的重连**。
        """
        client, self._client = self._client, None
        tracker, self._tracker = self._tracker, None
        ping, self._ping = self._ping, None
        if client is not None:
            # ① 先关掉自动重连，否则第 ② 步的取消会被当成断线。
            with suppress(Exception):
                client._auto_reconnect = False  # noqa: SLF001
        if tracker is not None:
            # ② 取消 SDK 派出去的全部任务（含 receive loop）。
            for task in tuple(tracker.tasks):
                task.cancel()
            if tracker.tasks:
                await asyncio.gather(*tracker.tasks, return_exceptions=True)
        if ping is not None:
            ping.cancel()  # ③
            await asyncio.gather(ping, return_exceptions=True)
        if client is not None:
            with suppress(Exception):
                await client._disconnect()  # noqa: SLF001  # ④
        with suppress(Exception):
            import lark_oapi.ws.client as ws_module  # ⑤ 摘掉代理

            if isinstance(getattr(ws_module, "loop", None), _TaskTracker):
                ws_module.loop = asyncio.get_running_loop()
        self._http = None

    # ------------------------------------------------------------------ 内部

    def _handler(self, lark: Any) -> Any:  # boundary: lark 的 EventDispatcherHandler
        """注册事件。只有 `im.message.receive_v1` 有真正的逻辑。"""
        builder = lark.EventDispatcherHandler.builder("", "")
        builder = builder.register_p2_im_message_receive_v1(self._dispatch)
        for name in _QUIET_EVENTS:
            register = getattr(builder, name, None)
            if register is not None:
                builder = register(_ignore)
        return builder.build()

    def _dispatch(self, event: Any) -> None:  # boundary: lark 的事件对象
        """SDK 的事件回调是**同步**的。派一个任务回主 loop，不阻塞收包。

        **一条消息炸掉不该让 bot 下线**，因此整个回调包一层。
        """
        with suppress(Exception):
            asyncio.get_running_loop().create_task(self._on_event(event))


def _ignore(event: Any) -> None:  # boundary: lark 的事件对象
    """消费掉一个我们不关心的事件。见 `_QUIET_EVENTS`。"""
    return None


# boundary: 返回的三样都是 lark SDK 对象，本模块是它仅有的两个出口之一
def _import_lark(domain: str) -> tuple[Any, Any, Any]:
    """在 worker 线程里 import SDK，并**收拾掉它在 import 期建出来的野 loop**。

    `lark_oapi.ws.client` 在模块顶层就跑 `asyncio.get_event_loop()`。在没有 loop 的
    worker 线程里那句会抛 `RuntimeError`，SDK 于是走 `except` 分支
    `new_event_loop()` + `set_event_loop()`——留下一个**永远不会被跑、也不会被关**的 loop，
    还被设在了线程池的某个 worker 上。不收拾的话它会一直挂着（并在解释器退出时报
    `ResourceWarning`）。

    在线程里 import 是必需的：`lark_oapi` 会拉进一整套生成代码，在主 loop 上做这件事会
    把冷启动卡住。
    """
    try:
        import lark_oapi as lark
        import lark_oapi.ws.client as ws_module
        from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN
    except ImportError as exc:
        raise KaryviaError(
            ErrorCode.EXTERNAL_CHANNEL, _MISSING_SDK, detail={"fix": MISSING_SDK_FIX}
        ) from exc
    stray = getattr(ws_module, "loop", None)
    if isinstance(stray, asyncio.AbstractEventLoop) and not stray.is_running():
        with suppress(Exception):
            stray.close()
    ws_module.loop = None
    with suppress(Exception):
        asyncio.set_event_loop(None)
    return lark, ws_module, LARK_DOMAIN if domain == "lark" else FEISHU_DOMAIN


def payload_of(event: Any) -> dict[str, JsonValue]:  # boundary: lark 的事件对象
    """诊断用：把事件拍成一份可 JSON 化的浅描述。"""
    raw = event_to_raw(event)
    return {
        "message_id": raw.message_id,
        "chat_id": raw.chat_id,
        "chat_type": raw.chat_type,
        "msg_type": raw.msg_type,
    }
