"""`job.py` 与 `store.py` 的用例：编解码往返、原子写、损坏保全与版本闸门。"""

from __future__ import annotations

import json
from datetime import UTC, timedelta
from pathlib import Path

import pytest
from _cron_fakes import EPOCH, KEY, Clock, make_job
from karyvia_plugin_cron.job import (
    MAX_HISTORY,
    RunRecord,
    RunStatus,
    ScheduleKind,
    decode_job,
    encode_job,
    new_job_id,
)
from karyvia_plugin_cron.store import JOBS_FILE, SCHEMA_VERSION, JobStore

from karyvia.contracts import ErrorCode, KaryviaError


def store_at(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / JOBS_FILE, now=Clock())


# ------------------------------------------------------------------------------ 编解码


@pytest.mark.parametrize(
    "job",
    [
        make_job(every_seconds=60),
        make_job(expr="0 9 * * 1-5", tz="Asia/Shanghai", every_seconds=None),
        make_job(at=EPOCH + timedelta(days=1), every_seconds=None),
    ],
)
def test_encode_decode_round_trip(job: object) -> None:
    assert decode_job(encode_job(job)) == job  # type: ignore[arg-type]


def test_round_trip_keeps_history() -> None:
    job = make_job().with_run(
        RunRecord(fired_at=EPOCH, status=RunStatus.DISPATCHED), next_run_at=None
    )
    restored = decode_job(encode_job(job))
    assert restored.history == job.history
    # 一次性推进到 `next_run_at=None` 会顺带停用它——这条也要能往返。
    assert restored.enabled is False


def test_history_is_capped() -> None:
    job = make_job()
    for index in range(MAX_HISTORY + 5):
        job = job.with_run(
            RunRecord(fired_at=EPOCH + timedelta(minutes=index), status=RunStatus.DISPATCHED),
            next_run_at=EPOCH + timedelta(minutes=index + 1),
        )
    assert len(job.history) == MAX_HISTORY


def test_timestamps_are_written_in_utc() -> None:
    """时刻统一换算成 UTC 再写，读的一方因此不必猜偏移的含义。"""
    payload = encode_job(make_job(created_at=EPOCH.astimezone(UTC)))
    assert str(payload["created_at"]).endswith("+00:00")


def test_unknown_fields_are_dropped_not_rejected() -> None:
    """一个被降级的实例不该因为新版本写过的字段而拒绝启动。"""
    payload = encode_job(make_job())
    payload["something_new"] = "from a future version"
    assert decode_job(payload).job_id == payload["id"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("id"),
        lambda p: p.pop("message"),
        lambda p: p.pop("origin"),
        lambda p: p.pop("schedule"),
        lambda p: p.pop("created_at"),
        lambda p: p.update(created_at="2026-08-15T09:00:00"),  # 缺时区
        lambda p: p.update(schedule={"kind": "whenever"}),
        lambda p: p.update(enabled="yes"),
    ],
)
def test_rejects_broken_records(mutate: object) -> None:
    payload = encode_job(make_job())
    mutate(payload)  # type: ignore[operator]
    with pytest.raises(KaryviaError):
        decode_job(payload)


def test_broken_history_entries_are_skipped_not_fatal() -> None:
    """历史是诊断数据。为一条坏记录拒绝加载一个还在排期的任务，代价与收益不成比例。"""
    payload = encode_job(
        make_job().with_run(
            RunRecord(fired_at=EPOCH, status=RunStatus.DISPATCHED), next_run_at=EPOCH
        )
    )
    payload["history"] = [{"status": "nonsense"}, *payload["history"]]  # type: ignore[misc]
    assert len(decode_job(payload).history) == 1


def test_job_ids_are_unique() -> None:
    assert len({new_job_id() for _ in range(200)}) == 200


def test_schedule_describe_covers_all_three_shapes() -> None:
    assert "每 60 秒" in make_job(every_seconds=60).schedule.describe()
    assert "cron" in make_job(expr="0 9 * * *", every_seconds=None).schedule.describe()
    assert "一次性" in make_job(at=EPOCH, every_seconds=None).schedule.describe()


def test_cron_describe_mentions_the_timezone() -> None:
    described = make_job(expr="0 9 * * *", tz="Asia/Shanghai", every_seconds=None)
    assert "Asia/Shanghai" in described.schedule.describe()


# ------------------------------------------------------------------------------ 存储


async def test_missing_file_is_not_an_error(tmp_path: Path) -> None:
    """首次运行没有任务表，那是正常状态而不是错误。"""
    assert await store_at(tmp_path).load() == ()


async def test_save_then_load(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    jobs = (make_job(name="一"), make_job(name="二", key=KEY))
    await store.save(jobs)
    assert await store.load() == jobs


async def test_save_leaves_no_temporary_file(tmp_path: Path) -> None:
    """原子写用同目录临时文件，成功之后不该有残留。"""
    store = store_at(tmp_path)
    await store.save((make_job(),))
    assert [path.name for path in tmp_path.iterdir()] == [JOBS_FILE]


async def test_save_writes_the_version(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    await store.save(())
    document = json.loads((tmp_path / JOBS_FILE).read_text(encoding="utf-8"))
    assert document["version"] == SCHEMA_VERSION


async def test_future_version_is_refused(tmp_path: Path) -> None:
    """格式版本高于本实现即拒绝——不猜、不降级。"""
    path = tmp_path / JOBS_FILE
    path.write_text(json.dumps({"version": SCHEMA_VERSION + 1, "jobs": []}), encoding="utf-8")
    with pytest.raises(KaryviaError) as caught:
        await store_at(tmp_path).load()
    assert caught.value.code is ErrorCode.PERSISTENCE_READ_FAILED


@pytest.mark.parametrize(
    "content",
    [
        "{ not json",
        "[]",  # 顶层不是对象
        '{"version": 1, "jobs": "nope"}',
        '{"version": 1, "jobs": [{"id": "x"}]}',  # 缺必填字段
    ],
)
async def test_corrupt_file_is_preserved_and_load_fails(tmp_path: Path, content: str) -> None:
    """**不静默用空表继续**：那会在下一次保存时覆盖掉还能恢复的数据。"""
    path = tmp_path / JOBS_FILE
    path.write_text(content, encoding="utf-8")
    with pytest.raises(KaryviaError) as caught:
        await store_at(tmp_path).load()
    assert caught.value.code is ErrorCode.PERSISTENCE_READ_FAILED
    assert caught.value.detail["preserved"] is True
    backups = [item.name for item in tmp_path.iterdir() if ".corrupt-" in item.name]
    assert len(backups) == 1
    # 备份里是原样的坏内容，人工可恢复。
    assert (tmp_path / backups[0]).read_text(encoding="utf-8") == content
    assert not path.exists()


async def test_error_detail_carries_no_host_path(tmp_path: Path) -> None:
    """宿主机绝对路径进模型可见的错误就是泄漏（`builtins/tools_fs` 的同一条判定）。"""
    path = tmp_path / JOBS_FILE
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(KaryviaError) as caught:
        await store_at(tmp_path).load()
    assert str(tmp_path) not in json.dumps(dict(caught.value.detail), ensure_ascii=False)


async def test_save_creates_the_directory_on_first_write(tmp_path: Path) -> None:
    """目录在第一次写入时才建——不为一个可能永远不排期的插件动用户的磁盘。"""
    nested = tmp_path / "cron"
    store = JobStore(nested / JOBS_FILE, now=Clock())
    assert not nested.exists()
    await store.save((make_job(),))
    assert nested.exists()


async def test_at_schedule_survives_a_round_trip_through_disk(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    job = make_job(at=EPOCH + timedelta(days=2), every_seconds=None)
    await store.save((job,))
    (restored,) = await store.load()
    assert restored.schedule.kind is ScheduleKind.AT
    assert restored.schedule.at == job.schedule.at
