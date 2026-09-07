"""`runtime/access/` 的用例：三个受守卫的资源门面。

覆盖四组：路径守卫（含与 `builtins/tools_fs` 那份的逐条对照）、读写分离的文件门面、
受限子进程（真的起进程，因为「函数是对的」不等于「调用它的那条路径是对的」）、
以及带 SSRF 守卫的出网（全走 `httpx.MockTransport`，一个 socket 都不开）。

`tests/runtime/conftest.py` 的 autouse 闸门在这里同样生效：出网用例注入假解析器与替身
传输，因此它们连 DNS 都不碰。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest

from karyvia.builtins.tools_fs.paths import WorkspaceGuard
from karyvia.builtins.tools_shell.environ import BASELINE_NAMES as TOOL_BASELINE
from karyvia.builtins.tools_shell.process import DEFAULT_GRACE_MS as TOOL_GRACE_MS
from karyvia.contracts import ErrorCode, KaryviaError
from karyvia.runtime.access import (
    BASELINE_NAMES,
    DEFAULT_GRACE_MS,
    MAX_OUTPUT_CHARS,
    MAX_REDIRECTS,
    GuardedFileAccess,
    GuardedHttpAccess,
    GuardedShellAccess,
    PathGuard,
    address_is_blocked,
)

SENTINEL = "S3NT1NEL-do-not-leak-9f2a7c"


# ---------------------------------------------------------------- 路径守卫


#: 两份实现必须给出同一套判定的相对路径样本。**绝对路径不在表里**——那是刻意的差异，
#: 由 `test_absolute_paths_are_rejected_unlike_the_tool_guard` 单独钉住。
_SHARED_CASES = ("a.txt", "sub/b.txt", "./a.txt", "../outside.txt", "sub/../a.txt", "", "CON", "x\x00y")


@pytest.mark.parametrize("raw", _SHARED_CASES)
def test_path_guard_matches_the_fs_workspace_guard(tmp_path: Path, raw: str) -> None:
    """第三份实现与 `builtins/tools_fs` 那份逐条对照（`R2` 让它够不着，只能各写一份）。"""
    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    mine, theirs = PathGuard(root), WorkspaceGuard(root)

    assert _outcome(lambda: mine.resolve(raw)) == _outcome(lambda: theirs.resolve(raw))


def test_absolute_paths_are_rejected_unlike_the_tool_guard(tmp_path: Path) -> None:
    """契约（`sdk/api.py`）就是这么写的：根由实例 Workspace 决定，不由调用方决定。

    `tools_fs` 反过来接受绝对路径——它面对的是模型给的串，拒绝只会换来一串 `../`。
    """
    root = tmp_path / "ws"
    root.mkdir()
    inside = root / "a.txt"
    inside.write_bytes(b"x")

    assert WorkspaceGuard(root).resolve(str(inside)) == inside.resolve()
    with pytest.raises(KaryviaError) as caught:
        PathGuard(root).resolve(str(inside))
    assert caught.value.code is ErrorCode.INPUT_MALFORMED


def test_the_error_detail_never_leaks_the_host_path(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    with pytest.raises(KaryviaError) as caught:
        PathGuard(root).resolve("../outside.txt")
    assert caught.value.detail == {"path": "../outside.txt"}


# ---------------------------------------------------------------- 文件门面


def _fs(tmp_path: Path) -> GuardedFileAccess:
    root = tmp_path / "ws"
    root.mkdir(exist_ok=True)
    return GuardedFileAccess(root, plugin_id="probe")


async def test_reading_and_writing_round_trip(tmp_path: Path) -> None:
    access = _fs(tmp_path)
    await access.write_text("notes/a.txt", "你好")
    assert await access.read_text("notes/a.txt") == "你好"
    assert await access.list_dir("notes") == ("a.txt",)


async def test_a_missing_file_reports_only_the_relative_path(tmp_path: Path) -> None:
    access = _fs(tmp_path)
    with pytest.raises(KaryviaError) as caught:
        await access.read_text("nope.txt")
    assert caught.value.code is ErrorCode.PERSISTENCE_READ_FAILED
    assert caught.value.detail["path"] == "nope.txt"
    assert str(tmp_path) not in repr(dict(caught.value.detail))


async def test_writing_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    access = _fs(tmp_path)
    await access.write_text("a.txt", "x")
    assert [p.name for p in (tmp_path / "ws").iterdir()] == ["a.txt"]


# ------------------------------------------------- 二进制读写


async def test_bytes_round_trip(tmp_path: Path) -> None:
    access = _fs(tmp_path)
    payload = bytes(range(256))
    await access.write_bytes("blob.bin", payload)
    assert await access.read_bytes("blob.bin") == payload


async def test_read_bytes_is_the_only_way_to_get_the_original_bytes(tmp_path: Path) -> None:
    """`read_text` 用 `errors="replace"`，因此它对一个 PNG 也「成功」——只是内容变了。

    这条就是 `read_bytes` 存在的理由：在它之前，一个想读二进制的插件唯一的选择是
    绕过门面直接 `open()`。
    """
    access = _fs(tmp_path)
    payload = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0xFF, 0xFE])
    await access.write_bytes("img.png", payload)
    assert await access.read_bytes("img.png") == payload
    assert (await access.read_text("img.png")).encode("utf-8") != payload


async def test_bytes_writes_are_guarded_by_the_same_path_rules(tmp_path: Path) -> None:
    access = _fs(tmp_path)
    with pytest.raises(KaryviaError) as caught:
        await access.write_bytes("../escape.bin", b"x")
    assert caught.value.code is ErrorCode.PERMISSION_PATH_OUTSIDE_WORKSPACE


async def test_a_failed_bytes_write_leaves_no_temporary_file(tmp_path: Path) -> None:
    """写失败不得留下半份文件——目标是个目录，替换必然失败。"""
    access = _fs(tmp_path)
    (tmp_path / "ws" / "taken").mkdir()
    with pytest.raises(KaryviaError) as caught:
        await access.write_bytes("taken", b"x")
    assert caught.value.code is ErrorCode.PERSISTENCE_WRITE_FAILED
    assert [p.name for p in (tmp_path / "ws").iterdir()] == ["taken"]


# ---------------------------------------------------------------- 子进程


def _shell(tmp_path: Path, **kwargs: object) -> GuardedShellAccess:
    root = tmp_path / "ws"
    root.mkdir(exist_ok=True)
    return GuardedShellAccess(root, plugin_id="probe", **kwargs)  # pyright: ignore[reportArgumentType]


def test_shell_baseline_matches_the_builtin_tool() -> None:
    """两份白名单各写一份（`R4`），由这条对照钉住——名单漂移比共享名单更危险。"""
    assert BASELINE_NAMES == TOOL_BASELINE
    assert DEFAULT_GRACE_MS == TOOL_GRACE_MS


async def test_running_a_command_returns_its_output_and_code(tmp_path: Path) -> None:
    result = await _shell(tmp_path).run([sys.executable, "-c", "print('hi')"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "hi"
    assert not result.timed_out


async def test_a_non_zero_exit_is_a_result_not_a_failure(tmp_path: Path) -> None:
    """契约写死了这条：非零退出码在 `exit_code` 里，不抛。"""
    result = await _shell(tmp_path).run(
        [sys.executable, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"]
    )
    assert (result.exit_code, result.stderr.strip()) == (3, "bad")


async def test_a_command_that_cannot_start_folds_into_minus_one(tmp_path: Path) -> None:
    """-1 不在 0–255 内，因此「起不来」与「程序真的返回了 1」仍然分得开。"""
    result = await _shell(tmp_path).run(["definitely-not-a-real-binary-9f2a7c"])
    assert result.exit_code == -1
    assert "definitely-not-a-real-binary-9f2a7c" in result.stderr


async def test_a_timeout_returns_a_result_with_the_flag_set(tmp_path: Path) -> None:
    result = await _shell(tmp_path, grace_ms=200).run(
        [sys.executable, "-c", "import time; time.sleep(30)"], timeout_ms=300
    )
    assert result.timed_out


async def test_the_parent_environment_does_not_reach_the_child(tmp_path: Path) -> None:
    """哨兵走**真实子进程**打印自己的整个环境再搜——`build_environment()` 是对的
    不等于调用它的那条路径是对的（漏传 `env=` 单测看不见， 采用的相同判据）。
    """
    source = {**os.environ, "MY_SECRET_TOKEN": SENTINEL}
    result = await _shell(tmp_path, env_source=source).run(
        [sys.executable, "-c", "import os; print(dict(os.environ))"]
    )
    assert result.exit_code == 0
    assert SENTINEL not in result.stdout
    assert "MY_SECRET_TOKEN" not in result.stdout


async def test_the_default_cwd_is_the_root_and_cwd_is_guarded(tmp_path: Path) -> None:
    root = (tmp_path / "ws").resolve()
    (root / "sub").mkdir(parents=True, exist_ok=True)
    access = _shell(tmp_path)

    here = await access.run([sys.executable, "-c", "import os; print(os.getcwd())"])
    assert Path(here.stdout.strip()).resolve() == root

    with pytest.raises(KaryviaError) as caught:
        await access.run([sys.executable, "-c", "pass"], cwd="..")
    assert caught.value.code is ErrorCode.PERMISSION_PATH_OUTSIDE_WORKSPACE


async def test_an_empty_command_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(KaryviaError) as caught:
        await _shell(tmp_path).run([])
    assert caught.value.code is ErrorCode.INPUT_MALFORMED


# ---------------------------------------------------------------- 出网


@pytest.mark.parametrize(
    ("address", "blocked"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("10.1.2.3", True),
        ("192.168.0.7", True),
        ("172.16.0.1", True),
        ("169.254.169.254", True),  # 云元数据地址
        ("0.0.0.0", True),  # noqa: S104 - 这里断言的正是它被拒
        ("224.0.0.1", True),
        ("fd00::1", True),
        ("8.8.8.8", False),
        ("93.184.216.34", False),
        ("not-an-ip", True),
    ],
)
def test_the_ssrf_table_is_explicit(address: str, blocked: bool) -> None:
    assert bool(address_is_blocked(address)) is blocked


def _net(*, handler: object, resolver: object = None) -> GuardedHttpAccess:
    return GuardedHttpAccess(
        plugin_id="probe",
        resolver=resolver or (lambda host, port: ("93.184.216.34",)),  # pyright: ignore[reportUnknownLambdaType]

        transport=httpx.MockTransport(handler),  # pyright: ignore[reportArgumentType]
    )


async def test_a_public_target_goes_through() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-probe"] == "1"
        return httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})

    response = await _net(handler=handler).request(
        "GET", "https://example.com/a", headers={"x-probe": "1"}
    )
    assert (response.status, response.body) == (200, b"ok")
    assert response.headers["content-type"] == "text/plain"


async def test_a_non_2xx_is_a_result_not_an_exception() -> None:
    response = await _net(handler=lambda request: httpx.Response(503)).request(
        "GET", "https://example.com/"
    )
    assert response.status == 503


# --------------------------------------------------- 下载上界


async def test_max_bytes_truncates_and_says_so() -> None:
    body = b"x" * 5000
    response = await _net(handler=lambda request: httpx.Response(200, content=body)).request(
        "GET", "https://example.com/", max_bytes=100
    )
    assert (len(response.body), response.truncated) == (100, True)


async def test_a_body_exactly_at_the_cap_is_not_reported_as_truncated() -> None:
    """「正好等于上界」是完整内容。谎报会让调用方给模型加一句不实的省略提示。"""
    body = b"x" * 100
    response = await _net(handler=lambda request: httpx.Response(200, content=body)).request(
        "GET", "https://example.com/", max_bytes=100
    )
    assert (response.body, response.truncated) == (body, False)


async def test_without_a_cap_nothing_is_truncated() -> None:
    body = b"x" * 5000
    response = await _net(handler=lambda request: httpx.Response(200, content=body)).request(
        "GET", "https://example.com/"
    )
    assert (len(response.body), response.truncated) == (5000, False)


async def test_a_non_positive_cap_is_rejected() -> None:
    """`0` 不是「不限」——那是 `None`。静默把它当成不限就是无声地取消一道防线。"""
    with pytest.raises(KaryviaError) as caught:
        await _net(handler=lambda request: httpx.Response(200)).request(
            "GET", "https://example.com/", max_bytes=0
        )
    assert caught.value.code is ErrorCode.INPUT_MALFORMED


async def test_the_cap_survives_a_redirect() -> None:
    """上界作用在**最终**那次响应上，而重定向链上每一跳都仍然过守卫。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a":
            return httpx.Response(302, headers={"location": "https://example.com/b"})
        return httpx.Response(200, content=b"y" * 900)

    response = await _net(handler=handler).request(
        "GET", "https://example.com/a", max_bytes=50
    )
    assert (len(response.body), response.truncated) == (50, True)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/",
        "http://[::1]/",
        "http://192.168.1.10/",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
async def test_private_targets_are_denied(url: str) -> None:
    """守卫判的是**解析之后**的地址，因此域名伪装（`127.0.0.1.nip.io`）同样挡得住。"""
    access = _net(handler=lambda request: httpx.Response(200), resolver=_never_called)
    with pytest.raises(KaryviaError) as caught:
        await access.request("GET", url)
    assert caught.value.code is ErrorCode.PERMISSION_DENIED


async def test_a_hostname_resolving_inside_is_denied() -> None:
    access = _net(handler=lambda request: httpx.Response(200), resolver=lambda h, p: ("10.0.0.5",))
    with pytest.raises(KaryviaError) as caught:
        await access.request("GET", "http://127.0.0.1.nip.io/")
    assert "私有网段" in str(caught.value.detail["reason"])


async def test_one_public_address_does_not_excuse_a_private_one() -> None:
    """一个主机名可以解析出多个地址；**只要有一个越界就整体拒绝**，不挑能用的那个。"""
    access = _net(
        handler=lambda request: httpx.Response(200),
        resolver=lambda h, p: ("93.184.216.34", "127.0.0.1"),
    )
    with pytest.raises(KaryviaError):
        await access.request("GET", "https://example.com/")


async def test_a_redirect_into_the_private_range_is_denied() -> None:
    """交给 httpx 跟随重定向等于让第 2 跳绕过守卫。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})
        raise AssertionError("第二跳不该被发出去")

    with pytest.raises(KaryviaError) as caught:
        await _net(handler=handler).request("GET", "https://example.com/")
    assert caught.value.code is ErrorCode.PERMISSION_DENIED


async def test_a_public_redirect_is_followed() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/a":
            return httpx.Response(302, headers={"location": "/b"})
        return httpx.Response(200, content=b"done")

    response = await _net(handler=handler).request("GET", "https://example.com/a")
    assert response.body == b"done"
    assert [httpx.URL(url).path for url in seen] == ["/a", "/b"]


async def test_a_redirected_post_becomes_a_get_without_the_body() -> None:
    """RFC 9110：303 与「POST 收到 302」转成 GET。照做而不是原样重发——否则一次带 body
    的写请求会被悄悄发两次。"""
    methods: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append((request.method, request.content))
        if request.url.path == "/a":
            return httpx.Response(302, headers={"location": "/b"})
        return httpx.Response(200)

    await _net(handler=handler).request("POST", "https://example.com/a", body=b"payload")
    assert methods == [("POST", b"payload"), ("GET", b"")]


async def test_a_redirect_loop_is_cut_off() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/next"})

    with pytest.raises(KaryviaError) as caught:
        await _net(handler=handler).request("GET", "https://example.com/a")
    assert str(MAX_REDIRECTS) in str(caught.value.detail["reason"])


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "ftp://example.com/x", "https://user:pw@example.com/", "https:///x"],
)
async def test_malformed_or_credential_bearing_urls_are_denied(url: str) -> None:
    access = _net(handler=lambda request: httpx.Response(200), resolver=_never_called)
    with pytest.raises(KaryviaError) as caught:
        await access.request("GET", url)
    assert caught.value.code is ErrorCode.PERMISSION_DENIED


async def test_the_error_url_drops_the_query_string() -> None:
    """query 常常带着签名与一次性凭据，而 `detail` 会进事件流与日志。"""
    access = _net(handler=lambda request: httpx.Response(200), resolver=_never_called)
    with pytest.raises(KaryviaError) as caught:
        await access.request("GET", f"http://127.0.0.1/a?token={SENTINEL}")
    assert caught.value.detail["url"] == "http://127.0.0.1/a"
    assert SENTINEL not in repr(dict(caught.value.detail))


async def test_a_transport_failure_is_retryable_and_external() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(KaryviaError) as caught:
        await _net(handler=handler).request("GET", "https://example.com/")
    assert caught.value.code is ErrorCode.EXTERNAL_HTTP_REQUEST
    assert caught.value.retryable


async def test_a_timeout_maps_to_the_timeout_category() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(KaryviaError) as caught:
        await _net(handler=handler).request("GET", "https://example.com/")
    assert caught.value.code is ErrorCode.TIMEOUT_HTTP_REQUEST


def _never_called(host: str, port: int) -> tuple[str, ...]:
    raise AssertionError("这条用例不该走到名字解析")


def _outcome(run: object) -> object:
    """两份守卫的判定结果：成功交回 `True`，失败交回错误码。

    比对**结论**而不是路径本身——两份实现的根不同（`resolve()` 之后的绝对路径当然不同），
    要对齐的是「放行还是拒绝、拒绝时是哪一类」。
    """
    try:
        run()  # pyright: ignore[reportCallIssue]
    except KaryviaError as error:
        return error.code
    return True


# ---------------------------------------------------------------- 补覆盖


def test_relative_renders_posix_paths_and_rejects_outsiders(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    guard = PathGuard(root)

    assert guard.relative(guard.resolve("sub/a.txt")) == "sub/a.txt"
    assert guard.relative(root.resolve()) == "."
    with pytest.raises(KaryviaError) as caught:
        guard.relative(tmp_path / "elsewhere")
    # 递进来一个根外路径是本包内部的编程错误，不是用户输入问题。
    assert caught.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


async def test_output_beyond_the_cap_is_truncated(tmp_path: Path) -> None:
    """一条 `find /` 的输出足以撑爆内存；插件拿到的应当是能用的那部分而不是一次 OOM。"""
    access = _shell(tmp_path)
    size = MAX_OUTPUT_CHARS + 5_000
    result = await access.run([sys.executable, "-c", f"print('x' * {size})"])
    assert result.exit_code == 0
    assert "输出已截断" in result.stdout
    assert result.stdout.startswith("x" * 100)


async def test_a_process_that_ignores_the_terminate_signal_is_killed(tmp_path: Path) -> None:
    """三步收尾的最后一步：宽限期用尽就强杀，而不是永远等下去。"""
    ignore_sigterm = (
        "import signal, sys, time\n"
        "try:\n"
        "    signal.signal(signal.SIGTERM, lambda *a: None)\n"
        "except (AttributeError, ValueError, OSError):\n"
        "    pass\n"
        "time.sleep(30)\n"
    )
    result = await _shell(tmp_path, grace_ms=300).run(
        [sys.executable, "-c", ignore_sigterm], timeout_ms=300
    )
    assert result.timed_out


async def test_a_write_failure_is_reported_without_the_host_path(tmp_path: Path) -> None:
    """把一个目录当文件写：失败在落盘之前，且错误里只有插件自己给的那个相对串。"""
    access = _fs(tmp_path)
    (tmp_path / "ws" / "adir").mkdir()
    with pytest.raises(KaryviaError) as caught:
        await access.write_text("adir", "x")
    assert caught.value.code is ErrorCode.PERSISTENCE_WRITE_FAILED
    assert caught.value.detail["path"] == "adir"
    assert str(tmp_path) not in repr(dict(caught.value.detail))


async def test_listing_a_file_is_a_read_failure(tmp_path: Path) -> None:
    access = _fs(tmp_path)
    (tmp_path / "ws" / "a.txt").write_bytes(b"x")
    with pytest.raises(KaryviaError) as caught:
        await access.list_dir("a.txt")
    assert caught.value.code is ErrorCode.PERSISTENCE_READ_FAILED


def test_an_unresolvable_hostname_is_treated_as_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """解析不出来**不放行**：它在真正连接时可能解析成功（DNS 缓存、搜索域），
    放行等于把判定推给运气。"""
    import socket as socket_module

    from karyvia.runtime.access import net as net_module

    def boom(*args: object, **kwargs: object) -> object:
        raise OSError("no such host")

    monkeypatch.setattr(socket_module, "getaddrinfo", boom)
    assert net_module._resolve("nope.invalid", 443) == ("",)
    assert address_is_blocked("")
