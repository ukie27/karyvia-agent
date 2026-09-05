"""内建会话存储 `session_jsonl` 的验收（开发方案 `D17`）。

| 验收项 | 测试 |
| --- | --- |
| 通过 `SessionStoreContract` 全部用例 | `TestJsonlSessionStore` |
| 跨进程重启后历史可完整恢复（`SES-006`） | `TestCrossProcess` |
| 半写模拟：无半条记录、无半批（`EDG-504`） | `TestCrashRecovery` |
| 损坏不得伪装成空历史 | `TestCorruption` |
| 删除 / 过期 / 压缩的数据保留语义（`SES-005`） | `TestRetention` |
| 文档示例可被实现直接解析（防漂移） | `TestDocumentedFormat` |
| 文件名与 `InstanceLayout.session_paths()` 一致 | `test_filenames_match_the_instance_layout` |
| 内建以普通 manifest + `setup(api)` 注册（`BAS-005`） | `TestRegistration` |

两条写这些用例时的取舍：

- **崩溃用字节级模拟而不是真的 kill 进程**。要断言的是「格式对半写有免疫力」，那是
  `committed_bytes` 的性质，可以确定性地构造出来；真去 kill 一个子进程只能碰运气撞上
  那个窗口，失败时还分不清是实现坏了还是没撞上。跨进程那条则真的开子进程——那里要证明的
  恰好是「另一个进程能读懂」。
- **文档示例从 Markdown 里抠出来喂给解码器**。`SES-006` 承诺格式可被外部实现读取，而
  外部实现读的是文档不是源码。文档漂移在这里失败，比在某个用户的迁移脚本里失败好。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from karyvia.builtins.registry import BUILTIN_MANIFESTS, SESSION_JSONL
from karyvia.builtins.session_jsonl import (
    CAPABILITY_NAME,
    CONFIG_DIRECTORY_KEY,
    HISTORY_SUFFIX,
    META_FIELDS,
    META_SUFFIX,
    RECORD_FIELDS,
    JsonlSessionStore,
    decode_meta,
    decode_record,
    encode_record,
    resolve_directory,
)
from karyvia.builtins.session_jsonl.codec import _unfreeze
from karyvia.contracts import (
    SESSION_SCHEMA_VERSION,
    AttachmentRef,
    AttachmentSource,
    CapabilityKind,
    ErrorCode,
    KaryviaError,
    Role,
    SessionKey,
    SessionMessage,
)
from karyvia.kernel.config import InstanceLayout
from karyvia.sdk.testing import FakePluginContext, SessionStoreContract

DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "session-storage.md"

KEY = SessionKey(channel_id="cli", conversation_id="local")


def message(message_id: str, content: str = "hello", **kwargs: object) -> SessionMessage:
    return SessionMessage(
        message_id=message_id,
        role=Role.USER,
        content=content,
        created_at=datetime.now(UTC),
        **kwargs,  # type: ignore[arg-type]
    )


def summary(content: str = "前两条的摘要") -> SessionMessage:
    return SessionMessage(
        message_id="sum-1",
        role=Role.SYSTEM,
        content=content,
        created_at=datetime.now(UTC),
    )


def test_attachment_references_round_trip_in_the_current_format() -> None:
    attachment = AttachmentRef(
        source=AttachmentSource.URL,
        locator="https://files.example/report.pdf",
        media_type="application/pdf",
        size_bytes=42,
        filename="report.pdf",
    )
    original = message("m-file", attachments=(attachment,))
    assert decode_record(encode_record(original)) == original


# ------------------------------------------------------------------------------ 契约基类


class TestJsonlSessionStore(SessionStoreContract):
    """`SessionStoreContract` 的全部用例，跑在真实磁盘上。"""

    @pytest.fixture(autouse=True)
    def _use_tmp_directory(self, tmp_path: Path) -> None:
        self._directory = tmp_path / "sessions"

    def make_store(self) -> JsonlSessionStore:
        return JsonlSessionStore(self._directory)


# -------------------------------------------------------------------------------- 基本行为


class TestBasics:
    def store(self, tmp_path: Path) -> JsonlSessionStore:
        return JsonlSessionStore(tmp_path / "sessions")

    async def test_the_directory_is_created_on_first_write_not_on_construction(
        self, tmp_path: Path
    ) -> None:
        """只读命令（`karyvia capabilities`）不该因为一个从未用过的会话目录而留下痕迹。"""
        store = self.store(tmp_path)
        assert not store.directory.exists()
        assert (await store.load(KEY)).messages == ()
        assert await store.list_keys() == ()
        assert not store.directory.exists()

        await store.append(KEY, [message("m1")])
        assert store.directory.is_dir()

    async def test_an_empty_batch_does_not_create_a_session(self, tmp_path: Path) -> None:
        store = self.store(tmp_path)
        await store.append(KEY, [])
        assert await store.list_keys() == ()

    async def test_appends_preserve_order_across_batches(self, tmp_path: Path) -> None:
        store = self.store(tmp_path)
        await store.append(KEY, [message("m1"), message("m2")])
        await store.append(KEY, [message("m3")])
        snapshot = await store.load(KEY)
        assert [m.message_id for m in snapshot.messages] == ["m1", "m2", "m3"]

    async def test_content_with_newlines_stays_on_one_line(self, tmp_path: Path) -> None:
        """「一行一条」不能被消息内容破坏——JSON 转义是这个格式成立的全部依据。"""
        store = self.store(tmp_path)
        await store.append(KEY, [message("m1", "第一行\n第二行\r\n第三行")])
        history, _ = store.paths_for(KEY)
        assert len(history.read_text(encoding="utf-8").splitlines()) == 1
        snapshot = await store.load(KEY)
        assert snapshot.messages[0].content == "第一行\n第二行\r\n第三行"

    async def test_all_optional_fields_survive_a_round_trip(self, tmp_path: Path) -> None:
        store = self.store(tmp_path)
        record = SessionMessage(
            message_id="m-tool",
            role=Role.TOOL,
            content="42",
            created_at=datetime.now(UTC),
            turn_id="turn-7",
            tool_call_id="call-1",
            interrupted=True,
            metadata={"channel": {"edited": True}},
        )
        await store.append(KEY, [record])
        restored = (await store.load(KEY)).messages[0]
        assert restored == record

    async def test_different_session_keys_never_share_a_file(self, tmp_path: Path) -> None:
        """`EDG-203` 在存储层的形态：文件名只由 `storage_id()` 决定。"""
        store = self.store(tmp_path)
        first = SessionKey(channel_id="a", conversation_id="b:c")
        second = SessionKey(channel_id="a:b", conversation_id="c")
        await store.append(first, [message("m1", "第一个")])
        await store.append(second, [message("m2", "第二个")])
        assert (await store.load(first)).messages[0].content == "第一个"
        assert (await store.load(second)).messages[0].content == "第二个"
        assert set(await store.list_keys()) == {first, second}

    async def test_list_keys_skips_undecodable_filenames(self, tmp_path: Path) -> None:
        """一条坏文件名不得让 `/session` 与迁移工具整体失效。"""
        store = self.store(tmp_path)
        await store.append(KEY, [message("m1")])
        (store.directory / f"not-a-storage-id{HISTORY_SUFFIX}").write_text("{}", encoding="utf-8")
        assert await store.list_keys() == (KEY,)

    async def test_the_watermark_may_not_move_backwards(self, tmp_path: Path) -> None:
        store = self.store(tmp_path)
        await store.append(KEY, [message(f"m{index}") for index in range(4)])
        await store.compact(KEY, 2, summary())
        with pytest.raises(KaryviaError) as caught:
            await store.compact(KEY, 1, summary("更早的摘要"))
        assert caught.value.code is ErrorCode.INPUT_MALFORMED

    async def test_an_out_of_range_watermark_is_rejected(self, tmp_path: Path) -> None:
        store = self.store(tmp_path)
        await store.append(KEY, [message("m1")])
        with pytest.raises(KaryviaError) as caught:
            await store.compact(KEY, 9, summary())
        assert caught.value.code is ErrorCode.INPUT_MALFORMED


def test_filenames_match_the_instance_layout(tmp_path: Path) -> None:
    """存储层与实例布局对同一个会话必须给出同一对路径。

    `InstanceLayout.session_paths()`（`D10`）是实例目录的唯一来源，本模块是唯一的写入方。
    两边各写一份后缀，对不上时 `karyvia session` 会在一个空目录里找文件——而两处都「自洽」，
    没有任何单元测试会失败。所以对照断言只能写在这里。
    """
    layout = InstanceLayout(root=tmp_path)
    store = JsonlSessionStore(layout.sessions_dir)
    assert store.paths_for(KEY) == layout.session_paths(KEY.storage_id())
    assert (HISTORY_SUFFIX, META_SUFFIX) == (".jsonl", ".meta.json")


# ---------------------------------------------------------------------------- 跨进程恢复


_CHILD_APPEND = """
import asyncio, sys
from datetime import UTC, datetime
from karyvia.builtins.session_jsonl import JsonlSessionStore
from karyvia.contracts import Role, SessionKey, SessionMessage

store = JsonlSessionStore(sys.argv[1])
key = SessionKey(channel_id="cli", conversation_id="local")
messages = [
    SessionMessage(
        message_id=f"child-{index}",
        role=Role.USER,
        content=f"来自子进程 {index}",
        created_at=datetime.now(UTC),
    )
    for index in range(3)
]
asyncio.run(store.append(key, messages))
"""


class TestCrossProcess:
    async def test_history_written_by_another_process_is_fully_recovered(
        self, tmp_path: Path
    ) -> None:
        """`SES-006`：另一个进程写的历史，这个进程要能完整读回来。"""
        directory = tmp_path / "sessions"
        completed = subprocess.run(
            [sys.executable, "-c", _CHILD_APPEND, str(directory)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr

        store = JsonlSessionStore(directory)
        snapshot = await store.load(KEY)
        assert [m.message_id for m in snapshot.messages] == ["child-0", "child-1", "child-2"]
        assert snapshot.messages[2].content == "来自子进程 2"

        # 接着写：本进程的追加落在子进程写的内容之后，而不是覆盖它。
        await store.append(KEY, [message("m-local")])
        assert [m.message_id for m in (await store.load(KEY)).messages][-1] == "m-local"


# ------------------------------------------------------------------------------ 半写恢复


class TestCrashRecovery:
    """`EDG-504`：写到一半被杀死，重启后文件可解析、无半条记录、无半批。"""

    async def prepared(self, tmp_path: Path) -> JsonlSessionStore:
        store = JsonlSessionStore(tmp_path / "sessions")
        await store.append(KEY, [message("m1"), message("m2")])
        return store

    async def test_bytes_past_the_commit_watermark_are_ignored(self, tmp_path: Path) -> None:
        """崩在「写了字节、还没换 meta」之间：那一批整体不存在。"""
        store = await self.prepared(tmp_path)
        history, _ = store.paths_for(KEY)
        with open(history, "ab") as handle:
            handle.write(encode_record(message("m3")).encode("utf-8"))
            handle.write(b'{"message_id": "m4", "role": "us')  # 半条记录

        snapshot = await store.load(KEY)
        assert [m.message_id for m in snapshot.messages] == ["m1", "m2"]

    async def test_the_next_append_truncates_the_uncommitted_tail(self, tmp_path: Path) -> None:
        """半批不会一直挂在文件末尾——下一次追加把它截掉。"""
        store = await self.prepared(tmp_path)
        history, _ = store.paths_for(KEY)
        with open(history, "ab") as handle:
            handle.write(b'{"message_id": "m4", "role": "us')

        await store.append(KEY, [message("m5")])
        assert [m.message_id for m in (await store.load(KEY)).messages] == ["m1", "m2", "m5"]
        lines = history.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        assert all(json.loads(line) for line in lines)

    async def test_a_crashed_first_write_reads_as_an_empty_session(self, tmp_path: Path) -> None:
        """崩在第一批：`meta.json` 还不存在，会话就还不存在（而不是「有一条坏历史」）。"""
        store = JsonlSessionStore(tmp_path / "sessions")
        store.directory.mkdir(parents=True)
        history, meta = store.paths_for(KEY)
        history.write_bytes(b'{"message_id": "m1", "role": "us')
        assert not meta.exists()

        assert (await store.load(KEY)).messages == ()
        await store.append(KEY, [message("m1")])
        assert [m.message_id for m in (await store.load(KEY)).messages] == ["m1"]

    async def test_meta_is_never_half_written(self, tmp_path: Path) -> None:
        """`meta.json` 走原子替换，因此任何时刻读到的都是某次完整写入的结果。"""
        store = await self.prepared(tmp_path)
        _, meta_path = store.paths_for(KEY)
        meta = decode_meta(meta_path.read_text(encoding="utf-8"))
        assert meta.session_key == KEY
        assert meta.schema_version == SESSION_SCHEMA_VERSION
        history, _ = store.paths_for(KEY)
        assert meta.committed_bytes == history.stat().st_size
        assert not list(store.directory.glob("*.tmp"))


# -------------------------------------------------------------------------------- 损坏处理


class TestCorruption:
    """损坏必须报出来。静默返回空快照 = 一次读盘故障清空用户的上下文。"""

    async def prepared(self, tmp_path: Path) -> JsonlSessionStore:
        store = JsonlSessionStore(tmp_path / "sessions")
        await store.append(KEY, [message("m1"), message("m2")])
        return store

    async def test_a_broken_record_inside_the_watermark_raises(self, tmp_path: Path) -> None:
        store = await self.prepared(tmp_path)
        history, _ = store.paths_for(KEY)
        lines = history.read_text(encoding="utf-8").splitlines()
        # 长度不变，因此 `committed_bytes` 仍然指向文件末尾：这是「记录本身坏了」。
        broken = "x" * len(lines[0])
        history.write_text("\n".join([broken, lines[1]]) + "\n", encoding="utf-8")

        with pytest.raises(KaryviaError) as caught:
            await store.load(KEY)
        assert caught.value.code is ErrorCode.PERSISTENCE_RECORD_CORRUPT

    async def test_a_truncated_history_raises_instead_of_losing_records(
        self, tmp_path: Path
    ) -> None:
        """文件比水位短 = 有人截了它。少几条历史必须是错误，不是「就这些了」。"""
        store = await self.prepared(tmp_path)
        history, _ = store.paths_for(KEY)
        raw = history.read_bytes()
        history.write_bytes(raw[: len(raw) // 2])

        with pytest.raises(KaryviaError) as caught:
            await store.load(KEY)
        assert caught.value.code is ErrorCode.PERSISTENCE_RECORD_CORRUPT

    async def test_a_missing_history_file_raises_instead_of_reading_as_empty(
        self, tmp_path: Path
    ) -> None:
        """`meta.json` 说有 612 字节历史，而文件不见了——那不是「没有历史」。"""
        store = await self.prepared(tmp_path)
        history, _ = store.paths_for(KEY)
        history.unlink()

        with pytest.raises(KaryviaError) as caught:
            await store.load(KEY)
        assert caught.value.code is ErrorCode.PERSISTENCE_RECORD_CORRUPT

    async def test_a_history_that_is_not_utf8_raises(self, tmp_path: Path) -> None:
        store = await self.prepared(tmp_path)
        history, _ = store.paths_for(KEY)
        history.write_bytes(b"\xff\xfe" * (history.stat().st_size // 2))

        with pytest.raises(KaryviaError) as caught:
            await store.load(KEY)
        assert caught.value.code is ErrorCode.PERSISTENCE_RECORD_CORRUPT

    async def test_a_different_schema_version_is_rejected(self, tmp_path: Path) -> None:
        store = await self.prepared(tmp_path)
        _, meta_path = store.paths_for(KEY)
        record = json.loads(meta_path.read_text(encoding="utf-8"))
        record["schema_version"] = SESSION_SCHEMA_VERSION + 1
        meta_path.write_text(json.dumps(record), encoding="utf-8")

        with pytest.raises(KaryviaError) as caught:
            await store.load(KEY)
        assert caught.value.code is ErrorCode.PERSISTENCE_RECORD_CORRUPT
        assert caught.value.detail["schema_version"] == SESSION_SCHEMA_VERSION + 1

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda record: record.pop("compacted_through"), id="缺字段"),
            pytest.param(lambda record: record.update(committed_bytes=-1), id="负水位"),
            pytest.param(lambda record: record.update(session_key={}), id="坏 session_key"),
            pytest.param(lambda record: record.update(created_at="2026-08-12T09:30:00"), id="无时区"),
        ],
    )
    async def test_broken_meta_shapes_raise(self, tmp_path: Path, mutate: object) -> None:
        store = await self.prepared(tmp_path)
        _, meta_path = store.paths_for(KEY)
        record = json.loads(meta_path.read_text(encoding="utf-8"))
        mutate(record)  # type: ignore[operator]
        meta_path.write_text(json.dumps(record), encoding="utf-8")

        with pytest.raises(KaryviaError) as caught:
            await store.load(KEY)
        assert caught.value.code is ErrorCode.PERSISTENCE_RECORD_CORRUPT

    async def test_meta_that_is_not_utf8_raises(self, tmp_path: Path) -> None:
        store = await self.prepared(tmp_path)
        _, meta_path = store.paths_for(KEY)
        meta_path.write_bytes(b'{"schema_version": \xff}')

        with pytest.raises(KaryviaError) as caught:
            await store.load(KEY)
        assert caught.value.code is ErrorCode.PERSISTENCE_RECORD_CORRUPT

    async def test_a_watermark_beyond_the_records_raises(self, tmp_path: Path) -> None:
        """`compacted_through` 指向不存在的记录：`SessionSnapshot` 构造前就得拦下。"""
        store = await self.prepared(tmp_path)
        _, meta_path = store.paths_for(KEY)
        record = json.loads(meta_path.read_text(encoding="utf-8"))
        record["compacted_through"] = 99
        meta_path.write_text(json.dumps(record), encoding="utf-8")

        with pytest.raises(KaryviaError) as caught:
            await store.load(KEY)
        assert caught.value.code is ErrorCode.PERSISTENCE_RECORD_CORRUPT

    def test_a_record_violating_a_contract_invariant_is_corrupt_not_a_crash(self) -> None:
        """`tool_call_id` 只能出现在 `role=TOOL` 上——契约不变量的违反也是「记录坏了」。"""
        raw = json.dumps(
            {
                "message_id": "m1",
                "role": "user",
                "content": "hi",
                "created_at": datetime.now(UTC).isoformat(),
                "tool_call_id": "call-1",
            }
        )
        with pytest.raises(KaryviaError) as caught:
            decode_record(raw)
        assert caught.value.code is ErrorCode.PERSISTENCE_RECORD_CORRUPT

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("不是 JSON", id="非 JSON"),
            pytest.param("[1, 2]", id="不是对象"),
            pytest.param('{"role": "user", "content": "x", "created_at": "x"}', id="缺 message_id"),
            pytest.param(
                '{"message_id": "m", "role": "wizard", "content": "x", "created_at": "x"}',
                id="未知 role",
            ),
            pytest.param(
                '{"message_id": "m", "role": "user", "content": "x", "created_at": "昨天"}',
                id="坏时间戳",
            ),
            pytest.param(
                '{"message_id": "m", "role": "user", "content": "x",'
                ' "created_at": "2026-08-12T09:30:00"}',
                id="时间戳无时区",
            ),
            pytest.param(
                '{"message_id": "m", "role": "user", "content": "x",'
                ' "created_at": "2026-08-12T09:30:00+00:00", "metadata": 1}',
                id="metadata 不是对象",
            ),
            pytest.param(
                '{"message_id": "m", "role": "user", "content": "x",'
                ' "created_at": "2026-08-12T09:30:00+00:00", "interrupted": "yes"}',
                id="interrupted 不是布尔",
            ),
            pytest.param(
                '{"message_id": "m", "role": "user", "content": "x",'
                ' "created_at": "2026-08-12T09:30:00+00:00", "turn_id": 7}',
                id="可选字段类型不符",
            ),
            pytest.param(
                '{"message_id": "m", "role": "user", "content": "x",'
                ' "created_at": "2026-08-12T09:30:00+00:00", "attachments": {}}',
                id="attachments 不是数组",
            ),
            pytest.param(
                '{"message_id": "m", "role": "user", "content": "x",'
                ' "created_at": "2026-08-12T09:30:00+00:00", "attachments": [7]}',
                id="附件不是对象",
            ),
        ],
    )
    def test_broken_record_shapes_raise(self, raw: str) -> None:
        with pytest.raises(KaryviaError) as caught:
            decode_record(raw)
        assert caught.value.code is ErrorCode.PERSISTENCE_RECORD_CORRUPT

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("不是 JSON", id="非 JSON"),
            pytest.param('{"schema_version": true}', id="布尔冒充整数"),
            pytest.param(
                f'{{"schema_version": {SESSION_SCHEMA_VERSION}, "session_key": 7}}',
                id="session_key 不是对象",
            ),
            pytest.param(
                f'{{"schema_version": {SESSION_SCHEMA_VERSION},'
                ' "session_key": {"channel_id": "", "conversation_id": "c", "scope": "s"}}',
                id="session_key 分量非法",
            ),
        ],
    )
    def test_broken_meta_json_raises(self, raw: str) -> None:
        with pytest.raises(KaryviaError) as caught:
            decode_meta(raw)
        assert caught.value.code is ErrorCode.PERSISTENCE_RECORD_CORRUPT

    def test_encoding_a_non_json_value_is_a_type_error_not_a_silent_str(self) -> None:
        """`json.dumps` 的兜底钩子只解冻映射；别的东西必须炸，不能被 `str()` 蒙混过去。"""
        with pytest.raises(TypeError):
            json.dumps({"x": object()}, default=_unfreeze)


# ---------------------------------------------------------------------------- 数据保留语义


class TestRetention:
    """`SES-005`：删除、过期、压缩各自的数据保留语义，一条一条钉住。"""

    async def prepared(self, tmp_path: Path) -> JsonlSessionStore:
        store = JsonlSessionStore(tmp_path / "sessions")
        await store.append(KEY, [message(f"m{index}", f"内容 {index}") for index in range(4)])
        return store

    async def test_compaction_keeps_the_original_records_on_disk(self, tmp_path: Path) -> None:
        """压缩只是「不再送进模型」，不是删除——原文仍可被外部工具读到。"""
        store = await self.prepared(tmp_path)
        await store.compact(KEY, 2, summary())

        snapshot = await store.load(KEY)
        assert [m.message_id for m in snapshot.messages] == ["m0", "m1", "sum-1", "m2", "m3"]
        assert snapshot.compacted_through == 2
        assert [m.message_id for m in snapshot.live_messages] == ["sum-1", "m2", "m3"]

        history, _ = store.paths_for(KEY)
        text = history.read_text(encoding="utf-8")
        assert "内容 0" in text and "内容 1" in text

    async def test_compaction_is_idempotent_at_the_same_watermark(self, tmp_path: Path) -> None:
        """同一水位再压一次只是多插一条摘要，不会丢历史。"""
        store = await self.prepared(tmp_path)
        await store.compact(KEY, 2, summary())
        await store.compact(KEY, 2, summary("再压一次"))
        snapshot = await store.load(KEY)
        assert snapshot.compacted_through == 2
        assert [m.content for m in snapshot.live_messages][:2] == ["再压一次", "前两条的摘要"]

    async def test_deletion_removes_both_files_physically(self, tmp_path: Path) -> None:
        store = await self.prepared(tmp_path)
        history, meta = store.paths_for(KEY)
        assert history.exists() and meta.exists()

        assert await store.delete(KEY) is True
        assert not history.exists() and not meta.exists()
        assert await store.delete(KEY) is False
        assert await store.list_keys() == ()

    async def test_nothing_expires_on_its_own(self, tmp_path: Path) -> None:
        """本实现不做自动过期：陈旧会话必须原样读回，而不是「已经清理掉了」。"""
        store = await self.prepared(tmp_path)
        _, meta_path = store.paths_for(KEY)
        record = json.loads(meta_path.read_text(encoding="utf-8"))
        stale = datetime.now(UTC) - timedelta(days=900)
        record["created_at"] = record["updated_at"] = stale.isoformat()
        meta_path.write_text(json.dumps(record), encoding="utf-8")

        snapshot = await store.load(KEY)
        assert len(snapshot.messages) == 4
        assert snapshot.updated_at == stale
        # 读取不写盘：陈旧不是「等着被清理」的状态。
        assert json.loads(meta_path.read_text(encoding="utf-8"))["updated_at"] == stale.isoformat()


# ------------------------------------------------------------------------------ 文档不漂移


def _fenced_blocks(language: str) -> list[str]:
    text = DOC_PATH.read_text(encoding="utf-8")
    return re.findall(rf"```{language}\n(.*?)```", text, flags=re.DOTALL)


class TestDocumentedFormat:
    """`SES-006` 承诺的是**文档**可被外部实现读懂，所以断言的对象是文档里的字节。"""

    def test_the_documented_records_decode(self) -> None:
        blocks = _fenced_blocks("jsonl")
        assert blocks, "文档里的 jsonl 示例不见了"
        records = [
            decode_record(line) for block in blocks for line in block.splitlines() if line.strip()
        ]
        assert [record.message_id for record in records] == ["m-1", "m-2", "m-3", "m-4"]
        assert records[2].role is Role.TOOL and records[2].tool_call_id == "call-1"
        assert records[3].interrupted is True
        assert records[0].turn_id == "turn-7"
        # 可选字段缺席即默认值，这是文档承诺给外部实现的解码规则。
        assert records[0].tool_call_id is None and records[0].metadata == {}

    def test_the_documented_meta_decodes(self) -> None:
        blocks = _fenced_blocks("json")
        assert blocks, "文档里的 meta.json 示例不见了"
        assert set(json.loads(blocks[0])) == set(META_FIELDS)
        meta = decode_meta(blocks[0])
        assert meta.schema_version == SESSION_SCHEMA_VERSION
        assert meta.session_key == KEY
        assert meta.compacted_through == 0
        assert meta.committed_bytes == 612

    def test_the_encoded_field_names_are_frozen(self) -> None:
        """写出去的键必须落在已发布的清单内，且必填的四个一个不少。

        新增字段只能是可选的（`SES-006`）：外部实现按文档写的解码器不认识多出来的键。
        这条与文档的字段表是同一件事的两面，因此它在这里而不是在 `TestBasics` 里。
        """
        record = SessionMessage(
            message_id="m-tool",
            role=Role.TOOL,
            content="42",
            created_at=datetime.now(UTC),
            turn_id="turn-7",
            tool_call_id="call-1",
            interrupted=True,
            attachments=(
                AttachmentRef(
                    source=AttachmentSource.WORKSPACE,
                    locator="result.txt",
                    media_type="text/plain",
                ),
            ),
            metadata={"x": 1},
        )
        assert set(json.loads(encode_record(record))) == set(RECORD_FIELDS)
        minimal = json.loads(encode_record(message("m1")))
        assert set(minimal) == set(RECORD_FIELDS[:4])
        for field in RECORD_FIELDS:
            assert f"`{field}`" in DOC_PATH.read_text(encoding="utf-8")

    def test_the_documented_storage_ids_are_real(self) -> None:
        """文档举的三个文件名例子必须真的是 `storage_id()` 的输出。"""
        assert SessionKey("cli", "local").storage_id() == "cli~local~default"
        assert SessionKey("telegram", "-100123").storage_id() == "telegram~-100123~default"
        assert SessionKey("cli", "a:b", "proj").storage_id() == "cli~a%3Ab~proj"
        text = DOC_PATH.read_text(encoding="utf-8")
        for storage_id in ("cli~local~default", "telegram~-100123~default", "cli~a%3Ab~proj"):
            assert storage_id in text


# ------------------------------------------------------------------------------ IO 故障


class TestIoFailures:
    """`SES-003`：持久化失败不得伪装成成功。每条 IO 路径都要能把 `OSError` 报出来。

    用 monkeypatch 造故障，而不是靠只读目录或磁盘配额：后者在 Windows、Linux 与 CI 容器
    上的表现各不相同，而这里要断言的只是「`OSError` 被折成哪一个错误码」。
    """

    def store(self, tmp_path: Path) -> JsonlSessionStore:
        return JsonlSessionStore(tmp_path / "sessions")

    async def test_a_failing_mkdir_is_a_write_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_: object, **__: object) -> None:
            raise OSError(13, "denied")

        monkeypatch.setattr(Path, "mkdir", boom)
        with pytest.raises(KaryviaError) as caught:
            await self.store(tmp_path).append(KEY, [message("m1")])
        assert caught.value.code is ErrorCode.PERSISTENCE_WRITE_FAILED

    async def test_a_failing_history_write_is_a_write_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = self.store(tmp_path)
        store.directory.mkdir(parents=True)

        def boom(*_: object, **__: object) -> None:
            raise OSError(28, "no space")

        monkeypatch.setattr("karyvia.builtins.session_jsonl.store.open", boom, raising=False)
        with pytest.raises(KaryviaError) as caught:
            await store.append(KEY, [message("m1")])
        assert caught.value.code is ErrorCode.PERSISTENCE_WRITE_FAILED

    async def test_a_failing_meta_read_is_a_read_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = self.store(tmp_path)
        await store.append(KEY, [message("m1")])

        def boom(*_: object, **__: object) -> str:
            raise OSError(5, "io error")

        monkeypatch.setattr(Path, "read_text", boom)
        with pytest.raises(KaryviaError) as caught:
            await store.load(KEY)
        assert caught.value.code is ErrorCode.PERSISTENCE_READ_FAILED

    async def test_a_failing_history_read_is_a_read_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = self.store(tmp_path)
        await store.append(KEY, [message("m1")])

        def boom(*_: object, **__: object) -> bytes:
            raise OSError(5, "io error")

        monkeypatch.setattr(Path, "read_bytes", boom)
        with pytest.raises(KaryviaError) as caught:
            await store.load(KEY)
        assert caught.value.code is ErrorCode.PERSISTENCE_READ_FAILED

    async def test_a_failing_delete_is_a_write_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = self.store(tmp_path)
        await store.append(KEY, [message("m1")])

        def boom(*_: object, **__: object) -> None:
            raise OSError(13, "denied")

        monkeypatch.setattr(Path, "unlink", boom)
        with pytest.raises(KaryviaError) as caught:
            await store.delete(KEY)
        assert caught.value.code is ErrorCode.PERSISTENCE_WRITE_FAILED

    async def test_a_failing_listing_is_a_read_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = self.store(tmp_path)
        await store.append(KEY, [message("m1")])

        def boom(*_: object, **__: object) -> list[Path]:
            raise OSError(5, "io error")

        monkeypatch.setattr(Path, "glob", boom)
        with pytest.raises(KaryviaError) as caught:
            await store.list_keys()
        assert caught.value.code is ErrorCode.PERSISTENCE_READ_FAILED

    async def test_a_failing_meta_write_is_a_write_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """历史写成了、`meta.json` 没写成：那一批因此整体不生效，且必须报出来。"""
        store = self.store(tmp_path)

        def boom(*_: object, **__: object) -> None:
            raise OSError(28, "no space")

        monkeypatch.setattr("karyvia.builtins.session_jsonl.store._atomic_write", boom)
        with pytest.raises(KaryviaError) as caught:
            await store.append(KEY, [message("m1")])
        assert caught.value.code is ErrorCode.PERSISTENCE_WRITE_FAILED

        # 水位没推进 -> 那一批不存在，而不是「写了一半」。
        monkeypatch.undo()
        assert (await store.load(KEY)).messages == ()

    async def test_a_failing_rewrite_during_compaction_is_a_write_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = self.store(tmp_path)
        await store.append(KEY, [message("m1"), message("m2")])

        def boom(*_: object, **__: object) -> None:
            raise OSError(28, "no space")

        monkeypatch.setattr("karyvia.builtins.session_jsonl.store.open", boom, raising=False)
        with pytest.raises(KaryviaError) as caught:
            await store.compact(KEY, 1, summary())
        assert caught.value.code is ErrorCode.PERSISTENCE_WRITE_FAILED
        # 原子写失败不得留下临时文件。
        assert not list(store.directory.glob("*.tmp"))


# -------------------------------------------------------------------------------- 注册路径


class TestRegistration:
    """内建的落地形态：一份普通 manifest + 一个 `setup(api)`，没有第二条路（`BAS-005`）。"""

    def test_the_manifest_is_listed_as_a_builtin(self) -> None:
        assert SESSION_JSONL in BUILTIN_MANIFESTS
        assert SESSION_JSONL.id == "session-jsonl"
        declaration = SESSION_JSONL.capabilities[0]
        assert declaration.kind is CapabilityKind.SESSION_STORE
        assert declaration.name == CAPABILITY_NAME
        assert declaration.overrides is None
        # `priority` 不写：内建基准是 0，写了（哪怕写的是默认值 100）就会被原样采纳。
        assert "priority" not in declaration.model_fields_set

    def test_the_directory_falls_back_to_the_plugin_state_dir(self, tmp_path: Path) -> None:
        ctx = FakePluginContext(state_dir=tmp_path / "state")
        assert resolve_directory(ctx) == tmp_path / "state"

    @pytest.mark.parametrize("configured", [123, "", "   ", []])
    def test_a_bad_directory_setting_is_a_config_error(self, configured: object) -> None:
        """静默忽略一个写错类型的路径，会让会话安静地写到别处去。"""
        ctx = FakePluginContext(config={CONFIG_DIRECTORY_KEY: configured})  # type: ignore[dict-item]
        with pytest.raises(KaryviaError) as caught:
            resolve_directory(ctx)
        assert caught.value.code is ErrorCode.CONFIG_INVALID
