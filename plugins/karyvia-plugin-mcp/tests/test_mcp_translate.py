"""纯函数用例：命名归一、线格式翻译、配置校验。

一个 IO 都不做，也**一个 `mcp` SDK 符号都不碰**——本插件的 106 条判定里绝大多数在这里
逐字节钉住。
"""

from __future__ import annotations

import pytest
from _mcp_fakes import READ_TOOL, WRITE_TOOL
from karyvia_plugin_mcp import (
    DEFAULT_PREFIX,
    RemoteResult,
    RemoteTool,
    assign_names,
    describe_tool,
    normalise_segment,
    render_result,
    resolve_settings,
    summarise_parts,
    tool_name,
    tool_parameters,
    truncate,
    with_credential,
)

from karyvia.contracts import ErrorCode, JsonValue, KaryviaError, ToolSpec


class TestNormalise:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("read_file", "read_file"),
            ("read-file", "read_file"),
            ("ReadFile", "readfile"),
            ("read file", "read_file"),
            ("read--file", "read_file"),
            ("_read_", "read"),
            ("read.file", "read_file"),
            ("2fa", "n2fa"),
            ("a/b:c", "a_b_c"),
        ],
    )
    def test_shapes(self, raw: str, expected: str) -> None:
        assert normalise_segment(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "___", "---", "。。"])
    def test_names_that_cannot_be_normalised(self, raw: str) -> None:
        assert normalise_segment(raw) == ""

    def test_it_never_truncates(self) -> None:
        """截断会把两个长名字变成同一个，而那正是本模块要报出来的那种撞车。"""
        long_name = "a" * 300
        assert normalise_segment(long_name) == long_name


class TestToolName:
    def test_three_segments(self) -> None:
        assert tool_name(DEFAULT_PREFIX, "files", "read-File") == "mcp.files.read_file"

    def test_the_result_matches_the_contract_shape(self) -> None:
        """契约的工具名式样是 `^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)*$`——归一化的
        全部意义就是让 `ToolSpec` 构造得出来。"""
        name = tool_name(DEFAULT_PREFIX, "my-server", "get/Thing")
        assert ToolSpec(name=name, description="x", parameters={"type": "object"}).name == name

    @pytest.mark.parametrize(
        ("prefix", "server", "tool"),
        [("", "files", "read"), ("mcp", "。", "read"), ("mcp", "files", "___")],
    )
    def test_any_unusable_segment_yields_nothing(
        self, prefix: str, server: str, tool: str
    ) -> None:
        assert tool_name(prefix, server, tool) == ""


class TestAssignNames:
    def test_a_clean_table(self) -> None:
        assignment = assign_names(DEFAULT_PREFIX, "files", [READ_TOOL, WRITE_TOOL])
        assert sorted(assignment.assigned) == ["mcp.files.read_file", "mcp.files.write_file"]
        assert assignment.collisions == {}

    def test_a_collision_disables_every_side(self) -> None:
        """选任何一边都是替用户做决定，而模型拿到一个「名字对得上、行为却是另一个工具」
        的调用比少一个工具危险得多（registry 对同名冲突的同一条判定）。"""
        tools = [RemoteTool(name="get-file", description=""), RemoteTool(name="get_file", description="")]
        assignment = assign_names(DEFAULT_PREFIX, "files", tools)
        assert assignment.assigned == {}
        assert assignment.collisions == {"mcp.files.get_file": ("get-file", "get_file")}

    def test_a_collision_does_not_take_down_its_neighbours(self) -> None:
        tools = [
            RemoteTool(name="get-file", description=""),
            RemoteTool(name="get_file", description=""),
            READ_TOOL,
        ]
        assignment = assign_names(DEFAULT_PREFIX, "files", tools)
        assert sorted(assignment.assigned) == ["mcp.files.read_file"]

    def test_an_unnormalisable_name_is_recorded_not_dropped(self) -> None:
        """静默丢掉会让用户在 `karyvia capabilities` 里怎么找都找不到它。"""
        assignment = assign_names(DEFAULT_PREFIX, "files", [RemoteTool(name="。。", description="")])
        assert assignment.rejected == ("。。",)
        assert assignment.assigned == {}


class TestToolParameters:
    def test_a_schema_passes_through(self) -> None:
        """kernel 的 `ToolInvoker._compile()` 会拿它做真正的校验——在中间改写它的语义
        只会让「模型看到的约束」与「实际生效的约束」分叉。"""
        raw: JsonValue = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }
        assert tool_parameters(raw) == raw

    def test_a_missing_type_is_filled_in(self) -> None:
        """契约要求最外层是 object；不少 server 省略它。补的是**缺失**，不是改写。"""
        assert tool_parameters({"properties": {}})["type"] == "object"

    @pytest.mark.parametrize("raw", [None, "nope", 5, []])
    def test_an_unreadable_schema_falls_back_to_an_empty_object(self, raw: JsonValue) -> None:
        """拒绝这个工具会让用户看着 server 里明明有的工具消失。"""
        assert tool_parameters(raw) == {"type": "object", "properties": {}}


class TestDescribeTool:
    def test_it_names_the_server_and_the_original_name(self) -> None:
        """模型看到的是归一化之后的本地名，用户在 server 那边看到的是原名。"""
        text = describe_tool("files", "read-File", "读一个文件")
        assert "files" in text and "read-File" in text and "读一个文件" in text

    def test_an_empty_description_says_so(self) -> None:
        assert "未提供说明" in describe_tool("files", "x", "   ")


class TestSummariseParts:
    def test_text_parts_are_joined(self) -> None:
        text, attachments = summarise_parts(
            [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        )
        assert (text, attachments) == ("a\nb", ())

    def test_an_image_part_is_announced_not_dropped(self) -> None:
        """静默丢掉会让模型以为工具什么都没返回。"""
        _, attachments = summarise_parts([{"type": "image", "mimeType": "image/png"}])
        assert len(attachments) == 1
        assert "image/png" in attachments[0]

    def test_a_resource_part_carries_its_uri(self) -> None:
        _, attachments = summarise_parts(
            [{"type": "resource", "resource": {"uri": "file:///a"}}]
        )
        assert "file:///a" in attachments[0]

    def test_an_unknown_part_type_is_reported_by_name(self) -> None:
        """不认识不等于不存在。"""
        _, attachments = summarise_parts([{"type": "hologram"}])
        assert "hologram" in attachments[0]


class TestRenderResult:
    def test_text_and_attachments_are_both_shown(self) -> None:
        result = RemoteResult(text="done", attachments=("[图]",))
        assert render_result(result, 1000)[0] == "done\n[图]"

    def test_an_empty_result_says_so(self) -> None:
        """一个空字符串会让模型以为自己没看清。"""
        assert "没有返回任何内容" in render_result(RemoteResult(text=""), 1000)[0]

    def test_the_limit_is_respected(self) -> None:
        text, cut = render_result(RemoteResult(text="x" * 500), 120)
        assert cut is True
        assert len(text) <= 120


class TestTruncate:
    def test_short_text_is_untouched(self) -> None:
        assert truncate("abc", 10) == ("abc", False)

    def test_the_marker_counts_against_the_limit(self) -> None:
        text, cut = truncate("x" * 500, 120)
        assert (cut, len(text) <= 120) == (True, True)

    def test_a_limit_too_small_for_the_marker_yields_empty(self) -> None:
        assert truncate("x" * 50, 3) == ("", True)


class TestSettings:
    def test_an_empty_block_has_no_servers(self) -> None:
        assert resolve_settings({}).servers == ()

    def test_servers_are_sorted_by_name(self) -> None:
        """两次启动的注册顺序恒相同，与配置文件里的书写顺序无关。"""
        settings = resolve_settings(
            {
                "servers": {
                    "zeta": {"type": "stdio", "command": "a"},
                    "alpha": {"type": "stdio", "command": "b"},
                }
            }
        )
        assert [server.name for server in settings.servers] == ["alpha", "zeta"]

    def test_stdio_requires_a_command(self) -> None:
        error = _fails({"servers": {"files": {"type": "stdio"}}})
        assert error.detail["key"] == "plugins.mcp.config.servers.files.command"

    @pytest.mark.parametrize("transport", ["sse", "streamable_http"])
    def test_http_transports_require_a_url(self, transport: str) -> None:
        error = _fails({"servers": {"docs": {"type": transport}}})
        assert error.detail["key"] == "plugins.mcp.config.servers.docs.url"

    def test_an_unknown_transport_lists_the_choices(self) -> None:
        error = _fails({"servers": {"docs": {"type": "carrier-pigeon"}}})
        assert "choices" in error.detail

    def test_a_server_name_that_cannot_be_normalised_is_rejected(self) -> None:
        """它会成为工具名的第二段，归一不出来的名字在那里表达不了。"""
        error = _fails({"servers": {"My Server": {"type": "stdio", "command": "a"}}})
        assert error.detail["key"] == "plugins.mcp.config.servers.My Server"

    def test_disabled_servers_are_kept_but_not_enabled(self) -> None:
        """留在 `servers` 里而不是被摘掉——`enabled: false` 是用户的显式表达，
        诊断要看得见它。"""
        settings = resolve_settings(
            {"servers": {"files": {"type": "stdio", "command": "a", "enabled": False}}}
        )
        assert len(settings.servers) == 1
        assert settings.enabled_servers == ()

    def test_true_is_not_a_positive_integer(self) -> None:
        _fails({"call_timeout_ms": True})

    @pytest.mark.parametrize(
        "config",
        [
            {"servers": "nope"},
            {"servers": {"files": "nope"}},
            {"servers": {"files": {"type": "stdio", "command": "a", "args": "x"}}},
            {"servers": {"files": {"type": "stdio", "command": "a", "env": {"A": 1}}}},
            {"prefix": "Bad Prefix"},
            {"connect_timeout_ms": 0},
        ],
    )
    def test_rejected_shapes(self, config: dict[str, JsonValue]) -> None:
        _fails(config)


class TestCredential:
    def test_the_placeholder_is_replaced_in_headers(self) -> None:
        settings = resolve_settings(
            {
                "servers": {
                    "docs": {
                        "type": "sse",
                        "url": "https://x",
                        "headers": {"Authorization": "Bearer {api_key}"},
                    }
                }
            }
        )
        applied = with_credential(settings, "sk-token")
        assert applied.servers[0].headers["Authorization"] == "Bearer sk-token"

    def test_urls_and_args_are_left_alone(self) -> None:
        """凭据出现在进程命令行上会被 `ps` 看到，出现在 URL 里会进代理日志。"""
        settings = resolve_settings(
            {
                "servers": {
                    "docs": {"type": "sse", "url": "https://x/{api_key}"},
                    "files": {"type": "stdio", "command": "a", "args": ["{api_key}"]},
                }
            }
        )
        applied = with_credential(settings, "sk-token")
        by_name = {server.name: server for server in applied.servers}
        assert by_name["docs"].url == "https://x/{api_key}"
        assert by_name["files"].args == ("{api_key}",)


def _fails(config: dict[str, JsonValue]) -> KaryviaError:
    with pytest.raises(KaryviaError) as caught:
        resolve_settings(config)
    assert caught.value.code is ErrorCode.CONFIG_INVALID
    return caught.value
