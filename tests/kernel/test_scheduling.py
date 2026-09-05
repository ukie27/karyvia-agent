"""工具批次划分的单元测试（`D09`：`scheduling.py`）。

批次划分是**纯函数**，因此这里全部同步测试；真正的并发行为（是否重叠、完成顺序）在
`test_engine.py` 用 `asyncio.Barrier` 验证——用 barrier 而不是看时序痕迹，因为串行化时
barrier 会直接死锁，而时序痕迹在慢机器上会给出假阳性。
"""

from __future__ import annotations

from karyvia.contracts import Concurrency, ToolSpec
from karyvia.kernel.turn.scheduling import partition_tool_batches

from ._engine_support import tool_call, tool_spec


def _partition(*names: str, specs: dict[str, ToolSpec]) -> list[list[str]]:
    calls = [tool_call(name, call_id=f"c{index}") for index, name in enumerate(names)]
    return [[call.name for call in batch] for batch in partition_tool_batches(calls, specs)]


PARALLEL = {name: tool_spec(name) for name in "abc"}
EXCLUSIVE = {
    name: tool_spec(name, concurrency=Concurrency.EXCLUSIVE) for name in ("write", "shell")
}


def test_empty_calls_yield_no_batches() -> None:
    assert list(partition_tool_batches((), {})) == []


def test_parallel_tools_form_one_batch() -> None:
    assert _partition("a", "b", "c", specs=PARALLEL) == [["a", "b", "c"]]


def test_exclusive_tool_is_alone_in_its_batch() -> None:
    specs = PARALLEL | EXCLUSIVE
    assert _partition("write", "shell", specs=specs) == [["write"], ["shell"]]


def test_exclusive_tool_splits_surrounding_parallel_run() -> None:
    """`a b write c` 必须是 3 批：`write` 不能与任何东西重叠，但 `a b` 仍可并发。"""
    specs = PARALLEL | EXCLUSIVE
    assert _partition("a", "b", "write", "c", specs=specs) == [["a", "b"], ["write"], ["c"]]


def test_unknown_tool_is_treated_as_exclusive() -> None:
    """名字不在 spec 表里就无从判断它并发安全，按最保守的方式排——它接下来会被拒掉。"""
    assert _partition("a", "ghost", "b", specs=PARALLEL) == [["a"], ["ghost"], ["b"]]


def test_relative_order_is_preserved_across_batches() -> None:
    specs = PARALLEL | EXCLUSIVE
    batches = _partition("a", "write", "b", "shell", "c", specs=specs)
    assert batches == [["a"], ["write"], ["b"], ["shell"], ["c"]]
    assert [name for batch in batches for name in batch] == ["a", "write", "b", "shell", "c"]


def test_batches_are_immutable_sequences() -> None:
    calls = (tool_call("a"), tool_call("b"))
    batches = list(partition_tool_batches(calls, PARALLEL))
    assert isinstance(batches[0], tuple)
