"""阶段 A 三项机制的测试（`D27`；技术方案 §7.3，需求 `PLG-003`、`CMP-001`、`EDG-503`）。

四条主线：

- **拓扑序是确定的**：同一份输入每次得到同一个顺序（同层按 id 字典序），因为加载顺序
  必须可复现——它不决定覆盖（`EDG-102`），但它决定诊断读起来是不是同一回事。
- **三种落榜分得开**：依赖缺失、依赖成环（错误里带整条环路，`PLG-003`）、级联。
  把三者并成一句「依赖有问题」会让用户不知道该装什么还是该改什么。
- **配置校验带字段路径**（`CMP-001`），且指向 `config.json` 里的位置而不是插件私有
  schema 里的位置。
- **状态版本不一致时一个字节都不改写**（`EDG-503`）：旧状态要留得住。
"""

from __future__ import annotations

import json
from pathlib import Path

from karyvia.contracts import ErrorCode
from karyvia.kernel.plugins import (
    STATE_FILE,
    STATE_VERSION_KEY,
    PlanNode,
    check_state_version,
    plan_load_order,
    validate_plugin_config,
)

# ------------------------------------------------------------------------------ 拓扑序


def node(plugin_id: str, *dependencies: str) -> PlanNode:
    return PlanNode(plugin_id=plugin_id, dependencies=dependencies)


def test_no_nodes_is_an_ordinary_path() -> None:
    """未启用任何外部插件时计划为空（`PLG-007`、`EDG-101`）。"""
    plan = plan_load_order([])
    assert plan.order == () and plan.failures == ()


def test_dependencies_come_first() -> None:
    plan = plan_load_order([node("b", "a"), node("a"), node("c", "b")])
    assert plan.order == ("a", "b", "c")
    assert not plan.failures


def test_independent_plugins_are_ordered_by_id() -> None:
    """同层按字典序：同一份配置每次得到同一个顺序。"""
    assert plan_load_order([node("z"), node("m"), node("a")]).order == ("a", "m", "z")


def test_a_dependency_on_a_builtin_is_satisfied_by_provided() -> None:
    """依赖内建是合法的——内建在外部插件之前就注册完了，它们不参与排序。"""
    plan = plan_load_order([node("acme", "tools-fs")], provided=["tools-fs"])
    assert plan.order == ("acme",) and not plan.failures


def test_a_missing_dependency_is_a_phase_a_failure() -> None:
    plan = plan_load_order([node("acme", "nope")])
    assert plan.order == ()
    (failure,) = plan.failures
    assert failure.plugin_id == "acme"
    assert failure.error.code is ErrorCode.PLUGIN_LOAD_FAILED
    assert failure.error.detail["missing"] == ["nope"]


def test_a_cycle_is_reported_with_the_whole_path() -> None:
    """`PLG-003` 要求「指出环路」，因此断言的是整条环而不只是「有环」。"""
    plan = plan_load_order([node("a", "b"), node("b", "a")])
    assert plan.order == ()
    codes = {failure.plugin_id: failure.error for failure in plan.failures}
    assert set(codes) == {"a", "b"}
    assert codes["a"].detail["cycle"] == ["a", "b", "a"]


def test_a_self_loop_would_be_a_cycle_too() -> None:
    """manifest 层面禁止自依赖，机制层仍要能自圆其说（两处判定不互为前提）。"""
    (failure,) = plan_load_order([node("a", "a")]).failures
    assert failure.error.detail["cycle"] == ["a", "a"]


def test_dependents_of_a_failed_plugin_cascade() -> None:
    """依赖方随被依赖方一起落榜，且理由是「依赖自己没能加载」而不是「依赖不存在」。"""
    plan = plan_load_order([node("a", "nope"), node("b", "a")])
    assert plan.order == ()
    failures = {failure.plugin_id: failure.error for failure in plan.failures}
    assert failures["a"].detail["missing"] == ["nope"]
    assert failures["b"].detail["blocked_by"] == ["a"]


def test_excluded_plugins_keep_their_dependents_out() -> None:
    """已在别处判定落榜的插件不在这里再记一条，但它的依赖方仍然级联落榜。"""
    plan = plan_load_order([node("a"), node("b", "a"), node("c")], excluded=["a"])
    assert plan.order == ("c",)
    (failure,) = plan.failures
    assert failure.plugin_id == "b"
    assert failure.error.detail["blocked_by"] == ["a"]


def test_one_broken_plugin_does_not_take_the_others_down() -> None:
    """`PLG-004`：不相干的插件照常排进计划。"""
    plan = plan_load_order([node("a", "nope"), node("b")])
    assert plan.order == ("b",)


# ------------------------------------------------------------------------------ 配置校验

_SCHEMA = {
    "type": "object",
    "properties": {"retries": {"type": "integer"}},
    "required": ["retries"],
    "additionalProperties": False,
}


def test_no_schema_lets_any_config_through() -> None:
    """`config_schema` 是可选字段：没写它不等于「禁止一切键」。"""
    assert validate_plugin_config(None, {"anything": 1}, plugin_id="acme", pointer="/x") is None


def test_a_matching_config_passes() -> None:
    error = validate_plugin_config(
        _SCHEMA, {"retries": 3}, plugin_id="acme", pointer="/plugins/acme/config"
    )
    assert error is None


def test_a_wrong_type_is_reported_with_a_pointer_into_config_json() -> None:
    """`CMP-001`：路径指的是用户要去改的那个位置，而不是 schema 里的位置。"""
    error = validate_plugin_config(
        _SCHEMA, {"retries": "three"}, plugin_id="acme", pointer="/plugins/acme/config"
    )
    assert error is not None
    assert error.code is ErrorCode.CONFIG_INVALID
    problems = error.detail["problems"]
    assert isinstance(problems, list)
    assert problems[0]["pointer"] == "/plugins/acme/config/retries"


def test_a_missing_required_key_is_reported_at_the_block_itself() -> None:
    error = validate_plugin_config(
        _SCHEMA, {}, plugin_id="acme", pointer="/plugins/acme/config"
    )
    assert error is not None
    problems = error.detail["problems"]
    assert isinstance(problems, list)
    assert problems[0]["pointer"] == "/plugins/acme/config"


def test_a_broken_schema_blames_the_plugin_not_the_user() -> None:
    """schema 自己写错了是插件作者的 bug——两者的补救动作不同，错误码因此不同。"""
    error = validate_plugin_config(
        {"type": "nonsense"}, {}, plugin_id="acme", pointer="/plugins/acme/config"
    )
    assert error is not None
    assert error.code is ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED


def test_validation_never_raises_on_a_hostile_config() -> None:
    """约定不抛：插件配置写错时实例仍要能起来（`PLG-004`）。"""
    error = validate_plugin_config(
        _SCHEMA, {"retries": 1, "extra": {"deep": [1, 2]}}, plugin_id="acme", pointer="/p"
    )
    assert error is not None and error.code is ErrorCode.CONFIG_INVALID


# ------------------------------------------------------------------------------ 状态版本


def test_a_plugin_that_never_wrote_state_leaves_no_trace(tmp_path: Path) -> None:
    """状态目录不存在就什么都不做，**也不建目录**（与 `ctx.state_dir` 的惰性创建同一条约定）。"""
    state_dir = tmp_path / "acme"
    assert check_state_version(state_dir, 1, plugin_id="acme") is None
    assert not state_dir.exists()


def test_the_first_sighting_records_the_version(tmp_path: Path) -> None:
    state_dir = tmp_path / "acme"
    state_dir.mkdir()
    assert check_state_version(state_dir, 2, plugin_id="acme") is None
    document = json.loads((state_dir / STATE_FILE).read_text(encoding="utf-8"))
    assert document[STATE_VERSION_KEY] == 2


def test_a_matching_version_passes(tmp_path: Path) -> None:
    state_dir = tmp_path / "acme"
    state_dir.mkdir()
    check_state_version(state_dir, 2, plugin_id="acme")
    assert check_state_version(state_dir, 2, plugin_id="acme") is None


def test_a_changed_version_fails_and_keeps_the_old_state(tmp_path: Path) -> None:
    """`EDG-503`：升级失败要保住旧状态，因此标记文件一个字节都不许被改写。"""
    state_dir = tmp_path / "acme"
    state_dir.mkdir()
    check_state_version(state_dir, 1, plugin_id="acme")
    (state_dir / "data.jsonl").write_text("旧状态\n", encoding="utf-8")

    error = check_state_version(state_dir, 2, plugin_id="acme")

    assert error is not None
    assert error.code is ErrorCode.PLUGIN_LOAD_FAILED
    assert error.detail["declared"] == 2
    assert error.detail["recorded"] == 1
    document = json.loads((state_dir / STATE_FILE).read_text(encoding="utf-8"))
    assert document[STATE_VERSION_KEY] == 1
    assert (state_dir / "data.jsonl").read_text(encoding="utf-8") == "旧状态\n"


def test_a_downgrade_fails_too(tmp_path: Path) -> None:
    """升与降都拒绝：P0 没有迁移机制，两个方向都是拿用户数据赌一把。"""
    state_dir = tmp_path / "acme"
    state_dir.mkdir()
    check_state_version(state_dir, 3, plugin_id="acme")
    assert check_state_version(state_dir, 2, plugin_id="acme") is not None


def test_a_corrupt_marker_is_a_failure_not_a_crash(tmp_path: Path) -> None:
    state_dir = tmp_path / "acme"
    state_dir.mkdir()
    (state_dir / STATE_FILE).write_text("{不是 JSON", encoding="utf-8")
    error = check_state_version(state_dir, 1, plugin_id="acme")
    assert error is not None and error.code is ErrorCode.PLUGIN_LOAD_FAILED


def test_a_marker_of_the_wrong_shape_is_a_failure(tmp_path: Path) -> None:
    """`true` 是 `int` 的子类——布尔值不是版本号，这条用例盯着那个坑。"""
    state_dir = tmp_path / "acme"
    state_dir.mkdir()
    (state_dir / STATE_FILE).write_text(json.dumps({STATE_VERSION_KEY: True}), encoding="utf-8")
    assert check_state_version(state_dir, 1, plugin_id="acme") is not None
