"""`partition.py` 与 `scoring.py` 的用例：纯函数层，零 IO。

职责：分区映射的四条判定、记录标识的往返、分词与打分的可断言性质。
不负责：存储（`test_memory_store.py`）、四类能力（各自文件）。
"""

from __future__ import annotations

import pytest
from karyvia_plugin_memory.partition import (
    AGENT_PARTITION,
    RECALL_ORDER,
    RECORD_ID_SEPARATOR,
    Partition,
    parse_record_id,
    partition_for,
    partitions_for,
    record_id,
)
from karyvia_plugin_memory.scoring import CJK_NGRAM, rank, score, tokenize

from karyvia.contracts import ErrorCategory, FragmentScope, KaryviaError, SessionKey

KEY = SessionKey(channel_id="cli", conversation_id="local", scope="proj")


# ---------------------------------------------------------------------------- 分区


def test_session_partition_uses_the_published_storage_encoding() -> None:
    """复用 `SessionKey.storage_id()` 而不是自己拼——那是已发布的、无碰撞的编码契约。"""
    assert partition_for(FragmentScope.SESSION, KEY).token == KEY.storage_id()


def test_workspace_partition_is_the_session_scope() -> None:
    assert partition_for(FragmentScope.WORKSPACE, KEY).token == "proj"


def test_agent_partition_ignores_the_session_key() -> None:
    """实例级分区对任何会话都是同一份——这正是「跨 Session 的长期记忆」的意思。"""
    other = SessionKey(channel_id="chat", conversation_id="group-1", scope="another")
    assert partition_for(FragmentScope.AGENT, KEY) == partition_for(FragmentScope.AGENT, other)
    assert partition_for(FragmentScope.AGENT, KEY).token == AGENT_PARTITION


def test_user_scope_is_refused_with_a_reason() -> None:
    """`USER` 不静默降级成别的分区：那会让群聊里 A 的记忆被召回给 B。"""
    with pytest.raises(KaryviaError) as caught:
        partition_for(FragmentScope.USER, KEY)
    assert caught.value.category is ErrorCategory.INVALID_INPUT
    assert caught.value.detail["scope"] == "user"
    assert caught.value.detail["supported"] == ["session", "workspace", "agent"]


def test_recall_order_goes_from_narrow_to_wide() -> None:
    """顺序本身是行为：同分时会话级排在实例级之前（`store._search_sync` 依赖它）。"""
    assert RECALL_ORDER == (FragmentScope.SESSION, FragmentScope.WORKSPACE, FragmentScope.AGENT)


def test_partitions_are_ordered_by_recall_order_not_by_the_argument() -> None:
    """配置里的书写顺序不该改变行为——那不是一个用户会认为存在的开关。"""
    reversed_input = (FragmentScope.AGENT, FragmentScope.SESSION)
    assert [p.scope for p in partitions_for(reversed_input, KEY)] == [
        FragmentScope.SESSION,
        FragmentScope.AGENT,
    ]


def test_partitions_deduplicate() -> None:
    doubled = (FragmentScope.AGENT, FragmentScope.AGENT)
    assert len(partitions_for(doubled, KEY)) == 1


def test_partition_filenames_are_distinct_across_scopes() -> None:
    """三类分区落在同一个目录里，文件名必须分得开。"""
    names = {partition_for(scope, KEY).filename for scope in RECALL_ORDER}
    assert len(names) == 3


# ------------------------------------------------------------------------ 记录标识


@pytest.mark.parametrize("scope", RECALL_ORDER)
@pytest.mark.parametrize("sequence", [0, 1, 999_999])
def test_record_id_round_trips(scope: FragmentScope, sequence: int) -> None:
    partition = partition_for(scope, KEY)
    decoded, decoded_sequence = parse_record_id(record_id(partition, sequence))
    assert decoded == partition
    assert decoded_sequence == sequence


def test_the_separator_never_appears_inside_a_partition_token() -> None:
    """`#` 不在 `storage_id()` 的安全字符集里，因此 `rpartition` 不可能切错位置。

    用一个分量里带各种刁钻字符的 key 来证明——那些字符会被百分号编码，`#` 编成 `%23`。
    """
    nasty = SessionKey(channel_id="a#b", conversation_id="c~d", scope="e%f")
    for scope in RECALL_ORDER:
        token = partition_for(scope, nasty).filename
        assert RECORD_ID_SEPARATOR not in token


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "no-separator",
        "agent-agent#",  # 缺序号
        "agent-agent#x",  # 序号不是数字
        "#5",  # 缺分区
        "nosuchscope-token#1",  # 未知 scope
        "user-someone#1",  # 合法的 FragmentScope，但本实现服务不了它
        "agent#1",  # 缺 token
    ],
)
def test_malformed_record_ids_are_rejected(bad: str) -> None:
    with pytest.raises(KaryviaError) as caught:
        parse_record_id(bad)
    assert caught.value.category is ErrorCategory.INVALID_INPUT


def test_malformed_record_id_detail_is_truncated() -> None:
    """一条超长参数不该原样进 `detail`（它会被记进事件日志）。"""
    with pytest.raises(KaryviaError) as caught:
        parse_record_id("x" * 5_000)
    assert len(str(caught.value.detail["record_id"])) <= 200


# ---------------------------------------------------------------------------- 分词


def test_latin_is_split_on_words() -> None:
    assert tokenize("Dark Mode is nice") == ("dark", "mode", "is", "nice")


def test_cjk_is_split_into_bigrams() -> None:
    assert tokenize("深色模式") == ("深色", "色模", "模式")
    assert all(len(token) == CJK_NGRAM for token in tokenize("深色模式"))


def test_a_single_cjk_character_still_produces_a_token() -> None:
    """否则查「书」永远查不到东西。"""
    assert tokenize("书") == ("书",)


def test_mixed_text_splits_each_run_by_its_own_rule() -> None:
    assert tokenize("用户偏好 dark-mode 主题") == (
        "用户",
        "户偏",
        "偏好",
        "dark",
        "mode",
        "主题",
    )


def test_fullwidth_and_case_are_normalised() -> None:
    """全角是从聊天窗口粘贴时的常态，把它当成另一个词会让检索莫名其妙地失灵。"""
    assert tokenize("Ｄａｒｋ") == tokenize("dark")


def test_punctuation_is_a_separator_not_a_token() -> None:
    assert tokenize("a, b; c!") == ("a", "b", "c")


def test_arabic_is_not_treated_as_ideographic() -> None:
    """`Lo` 类里也有**有**词边界的文字，按二元组切会毁掉它们的按词匹配。"""
    assert tokenize("مرحبا بالعالم") == ()


# ---------------------------------------------------------------------------- 打分


def test_an_empty_query_scores_everything_zero() -> None:
    assert score((), [("a",), ("b",)]) == (0.0, 0.0)


def test_a_document_that_repeats_the_term_scores_higher() -> None:
    """`tf`：反复提到的词更能代表这条记忆。"""
    once, twice = score(("x",), [("x", "y", "z"), ("x", "x", "z")])
    assert twice > once


def test_a_term_everyone_mentions_carries_less_weight() -> None:
    """`idf`：满篇都是的词几乎不带信息量，而它在朴素计数里最容易刷分。"""
    common = [("the", "a"), ("the", "b"), ("the", "c")]
    rare = [("zebra", "a"), ("the", "b"), ("the", "c")]
    assert score(("zebra",), rare)[0] > score(("the",), common)[0]


def test_length_normalisation_stops_the_longest_document_from_always_winning() -> None:
    short = ("hit", "a")
    long = ("hit", *(f"w{index}" for index in range(200)))
    short_score, long_score = score(("hit",), [short, long])
    assert short_score > long_score


def test_rank_returns_the_most_relevant_first() -> None:
    texts = ["春江潮水连海平", "锦瑟无端五十弦", "千里莺啼绿映红"]
    ranked = rank("锦瑟无端五十弦", texts, limit=3)
    assert ranked
    assert texts[ranked[0].index] == "锦瑟无端五十弦"


def test_rank_respects_the_limit() -> None:
    texts = [f"关键词 第{index}条" for index in range(10)]
    assert len(rank("关键词", texts, limit=3)) == 3


def test_rank_drops_documents_that_match_nothing() -> None:
    """0 分的记忆塞进上下文只是白占预算。"""
    assert rank("完全无关的查询", ["深色模式", "PostgreSQL"], limit=5) == ()


def test_rank_is_stable_within_the_same_score() -> None:
    """同分按输入顺序——`partitions_for` 的「由窄到宽」靠这条生效。"""
    texts = ["同样的内容", "同样的内容", "同样的内容"]
    ranked = rank("同样的内容", texts, limit=3)
    assert [item.index for item in ranked] == [0, 1, 2]


def test_min_score_is_a_gate() -> None:
    texts = ["深色模式"]
    assert rank("深色模式", texts, limit=5, min_score=0.0)
    assert rank("深色模式", texts, limit=5, min_score=1_000.0) == ()


def test_rank_with_no_texts_or_no_limit_is_empty() -> None:
    assert rank("x", [], limit=5) == ()
    assert rank("x", ["x"], limit=0) == ()


def test_a_single_document_can_still_score_above_zero() -> None:
    """`total == 1` 时 idf 的分子加一，否则一切都是 0 分（这条踩过）。"""
    assert rank("深色模式", ["深色模式"], limit=1)


def test_partition_is_hashable_and_comparable() -> None:
    """`partitions_for` 的去重与用例里的相等断言都依赖它是 frozen dataclass。"""
    one = Partition(FragmentScope.AGENT, "agent")
    assert one == Partition(FragmentScope.AGENT, "agent")
    assert len({one, Partition(FragmentScope.AGENT, "agent")}) == 1
