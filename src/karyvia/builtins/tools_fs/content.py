"""文本解码、二进制判定与截断（`EDG-205`、`TOL-003`、`EDG-403`、`NFR-605`）。

职责：把磁盘上的字节判成「能给模型看的文本」，以及把任何过长的结果收进上限内并如实
标注 `truncated`。
不负责：读写文件、决定上限取值（配置在 `settings.py`）、构造 `ToolResult`。

三条决定了本模块形状的规则：

- **二进制不解码**（`EDG-205`）。含 NUL 字节即判为二进制并整份拒绝：给模型一段乱码不会
  让它停下来，只会让它据此继续猜。
- **坏编码不整份拒绝**。不含 NUL 却不是合法 UTF-8 时用 `errors="replace"` 解码并如实
  标注——一个只坏了一行的日志文件仍然值得读，而损坏本身是可见的（`�`）。
- **换行在读的一侧归一**。`\r\n` 与孤立 `\r` 一律变成 `\n`；写的一侧原样落盘、不做任何
  翻译。两条合起来才是 `NFR-605`：同一份文件在 Windows 与 Linux 上给模型看到的是同一段
  文本，而工具写下去的字节在两个平台上完全相同。
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "BINARY_SNIFF_BYTES",
    "REPLACEMENT_CHAR",
    "decode_text",
    "looks_binary",
    "normalize_newlines",
    "truncate",
]

#: 判定二进制时嗅探的前缀长度。整份扫描对一个几十 MB 的文件没有意义——真正的二进制
#: 格式（可执行文件、图片、压缩包）几乎都在头部就有 NUL。
BINARY_SNIFF_BYTES: Final = 8192

#: `errors="replace"` 产出的字符。测试断言它出现，而不是断言「解码没抛异常」。
REPLACEMENT_CHAR: Final = "�"

#: 截断标记。`{shown}` / `{total}` 是字符数，不是字节数——模型消费的是字符。
_TRUNCATION_MARKER: Final = "\n… [truncated: 已显示 {shown}/{total} 字符]"


def looks_binary(data: bytes) -> bool:
    """含 NUL 字节即判为二进制。只看前 `BINARY_SNIFF_BYTES` 字节。"""
    return b"\x00" in data[:BINARY_SNIFF_BYTES]


def normalize_newlines(text: str) -> str:
    """`\\r\\n` 与孤立 `\\r` 一律归一成 `\\n`（`NFR-605`）。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def decode_text(data: bytes) -> tuple[str, bool]:
    """解码成文本，返回 `(文本, 是否有损)`。

    调用方应当**先**用 `looks_binary()` 挡掉二进制：本函数不做那个判断，它只负责
    「已经确定要当文本读了，怎么读」。
    """
    try:
        return normalize_newlines(data.decode("utf-8")), False
    except UnicodeDecodeError:
        return normalize_newlines(data.decode("utf-8", errors="replace")), True


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """收进 `limit` 字符内，返回 `(文本, 是否截断)`。

    **标记算在上限里**：返回值的长度恒 ≤ `limit`。先截到上限再拼一行标记会让结果比上限
    长，而上限的下游是契约的 `MAX_TOOL_RESULT_LENGTH`——那里超一个字符就构造失败。

    `limit` 小到连标记都放不下时返回空串 + `True`：那是配置写得不合理，但一个「说不清
    自己被截断了」的结果比一个空结果更危险。
    """
    total = len(text)
    if total <= limit:
        return text, False
    # 先按最坏情况（`shown` 等于上限，位数最多）算出能留多少字符，再用真实的 `shown`
    # 渲染标记。后者只会更短，因此 `keep + len(marker) <= limit` 恒成立，而标记里报的
    # 字符数与实际截出来的长度又是同一个数——两件事一次算清，不需要迭代。
    keep = limit - len(_TRUNCATION_MARKER.format(shown=limit, total=total))
    if keep <= 0:
        return "", True
    return text[:keep] + _TRUNCATION_MARKER.format(shown=keep, total=total), True
