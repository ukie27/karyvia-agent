"""错误契约：分类、稳定错误码与构造时脱敏的 `KaryviaError`。

职责：定义 `ErrorCategory` 的 11 个取值、集中登记的 `ErrorCode` 常量表，以及在**构造时**
完成脱敏的 `KaryviaError`；同时提供供 `events.py` 复用的纯函数 `redact` / `scrub`。
不负责：决定错误如何被记录、上报或重试——那是 `kernel/observability/` 与调用方的事；
也不负责判断某个值是否「真的」是密钥，只按键名、已知令牌形状与显式登记的密文判定。

三条约定：

- `code` 一次发布即为稳定契约，文档、测试和用户脚本可以依赖它，不得改名。
- `category` 由 `CODE_CATEGORIES` 从 `code` 推导，不接受调用方传入，杜绝同码异类。
- 异常堆栈只进日志，不进 `user_message`，也不进模型可见的 `ToolResult.content`。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, cast

if TYPE_CHECKING:  # pragma: no cover - 仅为注解，运行时不导入，避免与 ids.py 成环。
    from . import JsonValue
    from .capability import CapabilityRef
    from .ids import Correlation

__all__ = [
    "BENIGN_QUALIFIER_WORDS",
    "CODE_CATEGORIES",
    "MASK",
    "MAX_DETAIL_STRING_LENGTH",
    "SENSITIVE_KEY_BIGRAMS",
    "SENSITIVE_KEY_WORDS",
    "ErrorCategory",
    "ErrorCode",
    "KaryviaError",
    "SecretStr",
    "UnknownErrorCodeError",
    "key_words",
    "redact",
    "scrub",
]


class ErrorCategory(StrEnum):
    """诊断分类。11 个取值全部落地，不允许再加「其他」这种兜底项。"""

    INVALID_INPUT = "invalid_input"
    CONFIG = "config"
    CAPABILITY_MISSING = "capability_missing"
    PERMISSION_DENIED = "permission_denied"
    INCOMPATIBLE = "incompatible"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    EXTERNAL_SERVICE = "external_service"
    PLUGIN_FAILURE = "plugin_failure"
    PERSISTENCE = "persistence"
    KERNEL_INTERNAL = "kernel_internal"


class ErrorCode(StrEnum):
    """全部稳定错误码集中在此，禁止在其他模块出现错误码字面量。

    码值前缀是可读性分组，**不**用于推导分类——分类一律查 `CODE_CATEGORIES`。
    """

    # INVALID_INPUT
    INPUT_MALFORMED = "input.malformed"
    INPUT_TOO_LARGE = "input.too_large"
    INPUT_UNSUPPORTED_MEDIA = "input.unsupported_media"
    INPUT_SESSION_BUSY = "input.session_busy"

    # CONFIG
    CONFIG_INVALID = "config.invalid"
    CONFIG_UNKNOWN_FIELD = "config.unknown_field"
    CONFIG_SECRET_MISSING = "config.secret_missing"
    CONFIG_FILE_CORRUPT = "config.file_corrupt"
    CONFIG_INSTANCE_LOCKED = "config.instance_locked"

    # CAPABILITY_MISSING
    CAPABILITY_MISSING = "capability.missing"
    CAPABILITY_AMBIGUOUS = "capability.ambiguous"
    CAPABILITY_OVERRIDE_TARGET_MISSING = "capability.override_target_missing"

    # PERMISSION_DENIED
    PERMISSION_DENIED = "permission.denied"
    PERMISSION_PATH_OUTSIDE_WORKSPACE = "permission.path_outside_workspace"
    PERMISSION_TURN_REJECTED = "permission.turn_rejected"

    # INCOMPATIBLE
    PLUGIN_SDK_INCOMPATIBLE = "plugin.sdk_incompatible"
    PLUGIN_MANIFEST_UNSUPPORTED = "plugin.manifest_unsupported"

    # TIMEOUT
    TIMEOUT_MODEL_REQUEST = "timeout.model_request"
    TIMEOUT_TOOL_CALL = "timeout.tool_call"
    TIMEOUT_TOOL_CANCEL = "timeout.tool_cancel"
    TIMEOUT_HOOK = "timeout.hook"
    TIMEOUT_HTTP_REQUEST = "timeout.http_request"
    TIMEOUT_PLUGIN_STOP = "timeout.plugin_stop"
    TIMEOUT_TURN_COMPACTION = "timeout.turn_compaction"

    # CANCELLED
    CANCELLED_BY_USER = "cancelled.by_user"
    CANCELLED_BY_BUDGET = "cancelled.by_budget"
    CANCELLED_BY_SHUTDOWN = "cancelled.by_shutdown"

    # EXTERNAL_SERVICE
    EXTERNAL_MODEL_PROVIDER = "external.model_provider"
    EXTERNAL_MODEL_CONTEXT_OVERFLOW = "external.model_context_overflow"
    EXTERNAL_CHANNEL = "external.channel"
    EXTERNAL_HTTP_REQUEST = "external.http_request"
    EXTERNAL_TOOL_SERVER = "external.tool_server"

    # PLUGIN_FAILURE
    PLUGIN_LOAD_FAILED = "plugin.load_failed"
    PLUGIN_HOOK_FAILED = "plugin.hook_failed"
    PLUGIN_TURN_COMPACTION_FAILED = "plugin.turn_compaction_failed"
    PLUGIN_REGISTRATION_CONFLICT = "plugin.registration_conflict"
    CAPABILITY_OVERRIDE_CONFLICT = "capability.override_conflict"

    # PERSISTENCE
    PERSISTENCE_READ_FAILED = "persistence.read_failed"
    PERSISTENCE_WRITE_FAILED = "persistence.write_failed"
    PERSISTENCE_RECORD_CORRUPT = "persistence.record_corrupt"

    # KERNEL_INTERNAL
    KERNEL_INVARIANT_VIOLATED = "kernel.invariant_violated"
    KERNEL_UNEXPECTED = "kernel.unexpected"


#: 错误码到分类的唯一映射。新增错误码必须同时在此登记，否则 `KaryviaError` 构造失败。
CODE_CATEGORIES: Final[Mapping[ErrorCode, ErrorCategory]] = MappingProxyType(
    {
        ErrorCode.INPUT_MALFORMED: ErrorCategory.INVALID_INPUT,
        ErrorCode.INPUT_TOO_LARGE: ErrorCategory.INVALID_INPUT,
        ErrorCode.INPUT_UNSUPPORTED_MEDIA: ErrorCategory.INVALID_INPUT,
        # 「这个会话正忙」不是「你的消息太大」：前者的补救是等一会儿再发或改并发策略，
        # 后者的补救是把消息改短。复用 INPUT_TOO_LARGE 会让的队列满与
        #的大文本在诊断里长得一模一样。
        ErrorCode.INPUT_SESSION_BUSY: ErrorCategory.INVALID_INPUT,
        ErrorCode.CONFIG_INVALID: ErrorCategory.CONFIG,
        ErrorCode.CONFIG_UNKNOWN_FIELD: ErrorCategory.CONFIG,
        ErrorCode.CONFIG_SECRET_MISSING: ErrorCategory.CONFIG,
        ErrorCode.CONFIG_FILE_CORRUPT: ErrorCategory.CONFIG,
        # 「另一个实例正在跑」不是「你的配置写错了」：前者的补救是关掉那个进程或换实例目录，
        # 后者的补救是改文件。复用 CONFIG_INVALID 会让  无法单独断言。
        ErrorCode.CONFIG_INSTANCE_LOCKED: ErrorCategory.CONFIG,
        ErrorCode.CAPABILITY_MISSING: ErrorCategory.CAPABILITY_MISSING,
        ErrorCode.CAPABILITY_AMBIGUOUS: ErrorCategory.CAPABILITY_MISSING,
        # 覆盖目标不存在 = 那个能力真的不在，与 CAPABILITY_MISSING 同类。
        ErrorCode.CAPABILITY_OVERRIDE_TARGET_MISSING: ErrorCategory.CAPABILITY_MISSING,
        ErrorCode.PERMISSION_DENIED: ErrorCategory.PERMISSION_DENIED,
        ErrorCode.PERMISSION_PATH_OUTSIDE_WORKSPACE: ErrorCategory.PERMISSION_DENIED,
        # `turn_start` 拦截器返回 REJECT：这是一次**策略性拒绝**，不是插件故障。
        # 复用 PLUGIN_HOOK_FAILED 会把「插件按规则挡下了这次 turn」记成「插件坏了」，
        # 而两者的补救完全不同（改策略 vs 修插件）。先例是 INPUT_SESSION_BUSY。
        ErrorCode.PERMISSION_TURN_REJECTED: ErrorCategory.PERMISSION_DENIED,
        ErrorCode.PLUGIN_SDK_INCOMPATIBLE: ErrorCategory.INCOMPATIBLE,
        ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED: ErrorCategory.INCOMPATIBLE,
        ErrorCode.TIMEOUT_MODEL_REQUEST: ErrorCategory.TIMEOUT,
        ErrorCode.TIMEOUT_TOOL_CALL: ErrorCategory.TIMEOUT,
        # 「宽限期到了工具还没回来」与「工具按时超时并干净返回」是两件事：前者的
        # `side_effect` 只能是 UNKNOWN 且协程已被登记为孤儿任务，
        # 后者仍是一次有结论的调用。共用一个码就查不出哪些 turn 留下了在跑的副作用。
        ErrorCode.TIMEOUT_TOOL_CANCEL: ErrorCategory.TIMEOUT,
        ErrorCode.TIMEOUT_HOOK: ErrorCategory.TIMEOUT,
        # `ctx.net` 的出网超时。不复用 `TIMEOUT_TOOL_CALL`：发请求的可能是 Channel
        # 或 Hook，把它记成「某个工具超时了」会让诊断指向一个根本没被调用的工具。
        ErrorCode.TIMEOUT_HTTP_REQUEST: ErrorCategory.TIMEOUT,
        # 插件停止超时。不复用 `PLUGIN_LOAD_FAILED`：那是「它没能起来」，
        # 而这里是「它起来了、但没能在期限内停下」——后者留下的是仍在跑的后台任务，
        # 排查动作（看插件的 spawn_task 有没有吞掉 CancelledError）完全不同。
        ErrorCode.TIMEOUT_PLUGIN_STOP: ErrorCategory.TIMEOUT,
        ErrorCode.TIMEOUT_TURN_COMPACTION: ErrorCategory.TIMEOUT,
        ErrorCode.CANCELLED_BY_USER: ErrorCategory.CANCELLED,
        ErrorCode.CANCELLED_BY_BUDGET: ErrorCategory.CANCELLED,
        # 实例关闭导致的取消：既不是用户按下 Ctrl-C，也不是撞上预算，
        # 混进上面两个码会让「谁停掉了这个 turn」在诊断里不可判定。
        ErrorCode.CANCELLED_BY_SHUTDOWN: ErrorCategory.CANCELLED,
        ErrorCode.EXTERNAL_MODEL_PROVIDER: ErrorCategory.EXTERNAL_SERVICE,
        ErrorCode.EXTERNAL_MODEL_CONTEXT_OVERFLOW: ErrorCategory.EXTERNAL_SERVICE,
        ErrorCode.EXTERNAL_CHANNEL: ErrorCategory.EXTERNAL_SERVICE,
        # 同上：插件经 `ctx.net` 打出去的请求失败了。复用 `EXTERNAL_MODEL_PROVIDER` 会把
        # 一次 webhook 故障记到模型供应商头上。
        ErrorCode.EXTERNAL_HTTP_REQUEST: ErrorCategory.EXTERNAL_SERVICE,
        # 一个外部工具服务器（MCP server 一类）出了问题。它既不是 HTTP
        # 专属——stdio 传输一个字节的 HTTP 都没有——也不该混进 `EXTERNAL_CHANNEL`：
        # 后者说的是「消息发不出去」，而这里说的是「一次工具调用没能给出结论」。
        ErrorCode.EXTERNAL_TOOL_SERVER: ErrorCategory.EXTERNAL_SERVICE,
        ErrorCode.PLUGIN_LOAD_FAILED: ErrorCategory.PLUGIN_FAILURE,
        ErrorCode.PLUGIN_HOOK_FAILED: ErrorCategory.PLUGIN_FAILURE,
        ErrorCode.PLUGIN_TURN_COMPACTION_FAILED: ErrorCategory.PLUGIN_FAILURE,
        ErrorCode.PLUGIN_REGISTRATION_CONFLICT: ErrorCategory.PLUGIN_FAILURE,
        # 两个插件抢同一个覆盖目标，与 PLUGIN_REGISTRATION_CONFLICT 同类：
        # 都是「插件之间打架」，用户要做的是在配置里选一个。
        ErrorCode.CAPABILITY_OVERRIDE_CONFLICT: ErrorCategory.PLUGIN_FAILURE,
        ErrorCode.PERSISTENCE_READ_FAILED: ErrorCategory.PERSISTENCE,
        ErrorCode.PERSISTENCE_WRITE_FAILED: ErrorCategory.PERSISTENCE,
        ErrorCode.PERSISTENCE_RECORD_CORRUPT: ErrorCategory.PERSISTENCE,
        ErrorCode.KERNEL_INVARIANT_VIOLATED: ErrorCategory.KERNEL_INTERNAL,
        ErrorCode.KERNEL_UNEXPECTED: ErrorCategory.KERNEL_INTERNAL,
    }
)


# --------------------------------------------------------------------------- 脱敏

#: 掩码常量。脱敏后的值一律替换为它，便于测试与 sink 端识别。
MASK: Final = "***"

#: `detail` 中单个字符串的长度上限，超出部分截断并标注截断字符数。
MAX_DETAIL_STRING_LENGTH: Final = 512

#: 递归深度上限，防止构造错误时被深层结构拖死。
MAX_DETAIL_DEPTH: Final = 6

#: 短于此长度的密文不参与 `user_message` 反查替换，避免把 "id"、"key" 这类词全打码。
MIN_SCRUB_LENGTH: Final = 6

#: `SecretStr` 拒绝改写时的消息。提成常量只为让 `TRY003`（消息不写在 raise 处）满意。
_IMMUTABLE_MESSAGE: Final = "SecretStr 不可变"


class SecretStr:
    """密钥的包装类型：默认渲染为 `MASK`，明文只能经 `reveal()` 取出。

    落在 `contracts/errors.py` 而不是 `sdk/api.py`：`${VAR}` 的解析
    结果在 `kernel/config/secrets.py` 里产生，而 `R2` 禁止 `kernel/` import `sdk/`——与
     把 `CliEntry` 下沉到 `contracts/` 是同一条理由。落在本模块又是因为掩码、
    `redact()` 与它本就是同一件事的三个面：`_redact_value` 认得它，明文因此会进入
    `scrub()` 的密文集合，被顺手拼进消息里的凭据也擦得掉。

    它**不是**安全边界——`reveal()` 之后随手打印谁也拦不住。它防的是最常见的那条泄漏
    路径：把凭据拼进日志、prompt 或异常消息。`reveal()` 的调用点同时是审计与 grep 的
    抓手：明文从哪里开始流动是可查的。

    刻意**不用 dataclass**：`dataclasses.asdict()` 会把 `_value` 明文抖出来，且对任何
    *包含*它的 dataclass 递归时同样中招——那是一条绕过 `__repr__` 的泄漏路径。
    普通类对 `asdict()` 不透明。
    """

    __slots__ = ("_value",)

    #: 纯注解（不赋值），与 `__slots__` 并存不冲突；写出来是为了让类型检查器知道它是 `str`。
    _value: str

    def __init__(self, value: str) -> None:
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        """取出明文。调用点即「这里开始承担泄漏风险」的标记。"""
        return self._value

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(_IMMUTABLE_MESSAGE)

    def __delattr__(self, name: str) -> None:
        raise AttributeError(_IMMUTABLE_MESSAGE)

    def __str__(self) -> str:
        return MASK

    def __repr__(self) -> str:
        return f"SecretStr({MASK})"

    def __format__(self, format_spec: str) -> str:
        """任何格式规格都只得到掩码。

        不定义它的话 `f"{secret:>20}"` 会走 `object.__format__` 直接抛 `TypeError`——
        不泄漏，但会在无关的排版代码里炸出一个和密钥无关的报错。
        """
        return MASK

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SecretStr):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash((SecretStr, self._value))

    def __copy__(self) -> SecretStr:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> SecretStr:
        """不可变对象复制即自身。

        不定义它的话 `copy.deepcopy` 会走 `_reconstruct` 的 `setattr`，撞上上面的
        `__setattr__` 抛 `AttributeError`——而 `dataclasses.asdict()` 正是这条路径，
        「防泄漏」就会变成「在无关的地方炸掉」。
        """
        return self

#: 敏感键名单词。键名按 `_`、`-`、`.` 与 camelCase 边界切成单词后**整词**比对，
#: 不做子串匹配——子串匹配会把 `tokens`、`prompt_tokens` 这类用量统计一并打掉，
#: 而那正是可观测性最需要的信号。
#: 注意不含裸 "key"：`session_key`、`cache_key` 都不是密钥，打掉它们会让诊断失去主线索。
SENSITIVE_KEY_WORDS: Final[frozenset[str]] = frozenset(
    {
        "apikey",
        "authorization",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "passphrase",
        "passwd",
        "password",
        "secret",
        "secrets",
        "signature",
        "token",
    }
)

#: 敏感键名词组。裸 "key" 太宽，只有跟在这些限定词后面才算密钥。
SENSITIVE_KEY_BIGRAMS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("api", "key"),
        ("access", "key"),
        ("private", "key"),
        ("secret", "key"),
        ("signing", "key"),
        ("encryption", "key"),
    }
)

#: 统计类限定词。键名里出现它们说明这是用量/配额字段而不是密文本身，
#: 例如 `token_count`、`token_limit`、`token_usage`。
BENIGN_QUALIFIER_WORDS: Final[frozenset[str]] = frozenset(
    {"budget", "count", "limit", "limits", "remaining", "total", "usage", "used"}
)

#: 键名切词：连续大写、大写开头驼峰、纯小写、纯数字各成一段。
#: `"APIKey"` / `"apiKey"` / `"API_KEY"` 都切成 `("api", "key")`。
_KEY_WORD_PATTERN: Final = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|\d+")

#: 已知令牌形状。键名没命中但值长得像密钥时的第二道防线。
_SECRET_VALUE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
)


def _mask_known_shapes(text: str, found: set[str]) -> str:
    """把命中已知令牌形状的片段替换为掩码，并把原文登记进 `found`。"""
    for pattern in _SECRET_VALUE_PATTERNS:
        text = pattern.sub(lambda match: _record(match.group(0), found), text)
    return text


def _record(value: str, found: set[str]) -> str:
    found.add(value)
    return MASK


def key_words(key: str) -> tuple[str, ...]:
    """把键名切成小写单词序列。脱敏规则以此为准，测试可以直接断言切词结果。"""
    return tuple(word.lower() for word in _KEY_WORD_PATTERN.findall(key))


def _is_sensitive_key(key: str) -> bool:
    words = key_words(key)
    if any(word in BENIGN_QUALIFIER_WORDS for word in words):
        return False
    if any(word in SENSITIVE_KEY_WORDS for word in words):
        return True
    return any(bigram in SENSITIVE_KEY_BIGRAMS for bigram in zip(words, words[1:]))


def _collect_strings(value: object, found: set[str]) -> None:
    """递归登记被整体打码的值里出现的全部字符串，供 `user_message` 反查。"""
    if isinstance(value, SecretStr):
        found.add(value.reveal())
    elif isinstance(value, str):
        found.add(value)
    elif isinstance(value, Mapping):
        # isinstance 就是这次 cast 的运行时支撑；键类型不影响遍历值。
        for item in cast("Mapping[object, object]", value).values():
            _collect_strings(item, found)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in cast("Sequence[object]", value):
            _collect_strings(item, found)


def _redact_string(text: str, found: set[str]) -> str:
    masked = _mask_known_shapes(text, found)
    if len(masked) > MAX_DETAIL_STRING_LENGTH:
        dropped = len(masked) - MAX_DETAIL_STRING_LENGTH
        return f"{masked[:MAX_DETAIL_STRING_LENGTH]}…<truncated {dropped} chars>"
    return masked


def _redact_value(value: object, *, depth: int, found: set[str]) -> JsonValue:
    if depth > MAX_DETAIL_DEPTH:
        return "<depth-limit>"
    # 先于 str 判定：`SecretStr` 不是 str，但它必须在任何其它分支之前被认出来，
    # 否则会掉进末尾的 `<unsupported:...>`——不泄漏，却也丢掉了「这里有个密钥」的信息，
    # 更要紧的是明文进不了 `found`，`scrub()` 就擦不掉被拼进 `user_message` 的那一份。
    if isinstance(value, SecretStr):
        found.add(value.reveal())
        return MASK
    if isinstance(value, str):
        return _redact_string(value, found)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return _redact_mapping(cast("Mapping[str, object]", value), depth=depth, found=found)
    if isinstance(value, Sequence) and not isinstance(value, bytes):
        return [
            _redact_value(item, depth=depth + 1, found=found)
            for item in cast("Sequence[object]", value)
        ]
    # 非 JSON 形状的值不该进契约层；保留类型名以便定位构造点，但不保留内容。
    return f"<unsupported:{type(value).__name__}>"


def _redact_mapping(
    payload: Mapping[str, object], *, depth: int, found: set[str]
) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in payload.items():
        if _is_sensitive_key(key):
            _collect_strings(value, found)
            result[str(key)] = MASK
        else:
            result[str(key)] = _redact_value(value, depth=depth + 1, found=found)
    return result


def redact(payload: Mapping[str, object]) -> tuple[Mapping[str, JsonValue], frozenset[str]]:
    """返回脱敏后的只读映射，以及被摘除的原始密文集合。

    密文集合的用途是让调用方把同样的值从自由文本（`user_message`、事件摘要）里
    一并擦掉——只靠键名脱敏挡不住「密钥被顺手拼进了消息」这条泄漏路径。
    """
    found: set[str] = set()
    redacted = _redact_mapping(payload, depth=0, found=found)
    return MappingProxyType(redacted), frozenset(found)


def scrub(text: str, secrets: Iterable[str] = ()) -> str:
    """从自由文本中擦掉已知密文与已知令牌形状。

    长值优先替换，避免短密文先把长密文切碎导致残留片段。
    """
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) >= MIN_SCRUB_LENGTH:
            text = text.replace(secret, MASK)
    return _mask_known_shapes(text, set())


# --------------------------------------------------------------------------- 异常

class UnknownErrorCodeError(LookupError):
    """错误码没在 `CODE_CATEGORIES` 登记。这是编码错误，不是运行期可处理的失败，
    因此不走 `KaryviaError`——`KaryviaError` 自己就依赖这张表。"""

    def __init__(self, code: object) -> None:
        super().__init__(f"错误码未在 CODE_CATEGORIES 登记：{code!r}")


class KaryviaError(Exception):
    """Karyvia 的唯一错误契约：分类、稳定码、面向用户与面向诊断的两套信息。

    构造即完成脱敏：`user_message`、`detail` 与 `repr` 都不再含密文，因此把它扔给
    任何 sink、日志或模型可见文本都是安全的，脱敏不依赖下游是否记得处理。

    `capability` 指向出错的能力。它只在 `TYPE_CHECKING` 下导入——
    `capability.py` 运行时依赖本模块，反向导入会成环——因此这里不做形状校验：
    `CapabilityRef` 自己在构造时已经校验过，再验一遍只会在错误路径上多一个出错点。
    """

    __slots__ = (
        "_capability",
        "_category",
        "_code",
        "_correlation",
        "_detail",
        "_retryable",
        "_user_message",
    )

    def __init__(
        self,
        code: ErrorCode,
        user_message: str,
        *,
        detail: Mapping[str, object] | None = None,
        retryable: bool = False,
        correlation: Correlation | None = None,
        capability: CapabilityRef | None = None,
    ) -> None:
        category = CODE_CATEGORIES.get(code)
        if category is None:
            raise UnknownErrorCodeError(code)

        redacted, secrets = redact(detail or {})
        safe_message = scrub(user_message, secrets)

        self._code = code
        self._category = category
        self._user_message = safe_message
        self._detail = redacted
        self._retryable = retryable
        self._correlation = correlation
        self._capability = capability
        super().__init__(safe_message)

    @property
    def code(self) -> ErrorCode:
        """稳定错误码，发布后不可更改。"""
        return self._code

    @property
    def category(self) -> ErrorCategory:
        """由 `code` 推导，不接受调用方覆盖。"""
        return self._category

    @property
    def user_message(self) -> str:
        """面向用户的说明，已脱敏且不含堆栈。"""
        return self._user_message

    @property
    def detail(self) -> Mapping[str, JsonValue]:
        """面向诊断的结构化上下文，只读且已脱敏。"""
        return self._detail

    @property
    def retryable(self) -> bool:
        """同样的输入是否值得重试。由抛出点判断，不由调用方猜。"""
        return self._retryable

    @property
    def correlation(self) -> Correlation | None:
        """关联标识；在 turn 之外抛出（如实例启动阶段）时为 None。"""
        return self._correlation

    @property
    def capability(self) -> CapabilityRef | None:
        """出错的能力；与具体能力无关的错误（如配置解析）为 None。

         要求诊断能标明每项能力由内建还是插件提供，这个字段是那条信息在错误
        路径上的载体：`ref.provider` 直接回答「是谁的问题」。
        """
        return self._capability

    def with_correlation(self, correlation: Correlation) -> KaryviaError:
        """返回附带关联标识的新实例；契约对象不可变，因此不就地修改。"""
        return KaryviaError(
            self._code,
            self._user_message,
            detail=self._detail,
            retryable=self._retryable,
            correlation=correlation,
            capability=self._capability,
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self._code.value!r}, "
            f"category={self._category.value!r}, "
            f"user_message={self._user_message!r}, "
            f"retryable={self._retryable!r}, detail={dict(self._detail)!r})"
        )
