"""检索打分：分词、TF-IDF 式加权、取 top-N。**纯函数、零 IO、不认识任何契约类型。**

职责：把「一句查询 + 一堆候选文本」变成一个按相关性从高到低的下标序列。
不负责：读写文件（`store.py`）、决定查哪些分区（`partition.py`）、片段的形状（`record.py`）。

**为什么是自写打分而不是索引或向量。** 记忆条数是 10²–10³ 量级——全量扫描 + 打分在这个
规模上是毫秒级，而一个索引结构要维护增量更新、要处理删除后的重建、还要在崩溃后能重放。
向量检索则需要一次 embedding 调用，而插件今天发不起模型请求（`PluginContext` 没有这条
通道）。两者都是过早抽象（`AGENTS.md` 原则 4）。要换成向量后端，换掉整条 `MEMORY` 能力
即可——那正是 `MEM-001` 的意义。

**中文按字符二元组切，英文按词切。** 这是本模块唯一一个需要解释的决定：CJK 没有词边界，
按空白切会让「深色模式」整段变成一个 token，只有查询逐字一致才命中；按**单字**切又会让
「的」「了」这种字撑满候选。二元组是这两者之间那个不需要词典的折中——它不完美
（`AB|BC` 会让「模式深色」也部分命中「深色模式」），但它不需要引入分词器依赖，
而误召回的代价只是一条不太相关的记忆多占了几十个 token。
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from typing import Final

__all__ = [
    "CJK_NGRAM",
    "Scored",
    "rank",
    "score",
    "tokenize",
]

#: CJK 的切分粒度。改它会让既有记忆的召回结果整体变化，但**不会让数据不可读**——
#: 打分是每次查询现算的，没有落盘的索引。
CJK_NGRAM: Final = 2

#: 拉丁/数字词。`\w` 会把汉字也算进去，因此这里显式限定字符类。
_WORD_PATTERN: Final = re.compile(r"[a-z0-9_]+")

#: 单条候选参与打分的字符上限。一条超长记忆不该仅仅因为「字多、碰上的词也多」就排在前面；
#: 长度归一已经压了一部分，这个上界管住的是打分本身的代价。
_MAX_SCORED_CHARS: Final = 8_000


def _is_cjk(char: str) -> bool:
    """是否属于需要按字符切分的表意文字区。

    用 `unicodedata.category` 之外的显式码点区间：`Lo`（其他字母）里还有阿拉伯文、
    希伯来文这些**有**词边界的文字，把它们一并按二元组切会毁掉正常的按词匹配。
    """
    code = ord(char)
    return (
        0x3040 <= code <= 0x30FF  # 平假名 / 片假名
        or 0x3400 <= code <= 0x4DBF  # CJK 扩展 A
        or 0x4E00 <= code <= 0x9FFF  # CJK 基本区
        or 0xF900 <= code <= 0xFAFF  # 兼容表意文字
        or 0xAC00 <= code <= 0xD7AF  # 谚文音节
    )


def tokenize(text: str) -> tuple[str, ...]:
    """切词。拉丁按词、CJK 按 `CJK_NGRAM` 元组，其余字符作分隔符。

    先做 NFKC 归一化再小写：全角 `Ｄａｒｋ` 与半角 `dark` 是同一个词，而用户从聊天窗口
    粘过来的文本里全角相当常见。**单个 CJK 字符的文本仍产出一个 token**——否则查「书」
    永远查不到东西。
    """
    normalized = unicodedata.normalize("NFKC", text).lower()
    tokens: list[str] = []
    run: list[str] = []

    def flush_latin() -> None:
        if run:
            tokens.extend(_WORD_PATTERN.findall("".join(run)))
            run.clear()

    cjk_run: list[str] = []

    def flush_cjk() -> None:
        if not cjk_run:
            return
        if len(cjk_run) < CJK_NGRAM:
            tokens.append("".join(cjk_run))
        else:
            tokens.extend(
                "".join(cjk_run[index : index + CJK_NGRAM])
                for index in range(len(cjk_run) - CJK_NGRAM + 1)
            )
        cjk_run.clear()

    for char in normalized:
        if _is_cjk(char):
            flush_latin()
            cjk_run.append(char)
        else:
            flush_cjk()
            run.append(char)
    flush_latin()
    flush_cjk()
    return tuple(tokens)


class Scored:
    """一条候选的打分结果。下标指回调用方传进来的那个序列。

    刻意不是 dataclass 也不带候选本身：调用方已经有那个序列了，把它复制进结果只会让
    「同一份数据有两个持有者」。
    """

    __slots__ = ("index", "value")

    def __init__(self, index: int, value: float) -> None:
        self.index = index
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover - 只为测试失败时好读
        return f"Scored(index={self.index}, value={self.value:.4f})"


def score(query_tokens: Sequence[str], documents: Sequence[Sequence[str]]) -> tuple[float, ...]:
    """给每个候选打一个分。返回值与 `documents` 等长、一一对应。

    公式是 TF-IDF 的一个朴素形态：命中词的 `tf * idf` 求和，再除以 `sqrt(文档长度)`。
    三件事各自的作用——

    - `tf`（词在这条记忆里出现几次）：反复提到的词更能代表这条记忆。
    - `idf`（几条记忆提到过这个词）：满篇都是的词（「的」「the」「项目」）几乎不带信息量，
      而它们在朴素计数里恰好最容易刷分。
    - `sqrt` 长度归一：不做的话最长的那条记忆永远排第一。用 `sqrt` 而不是直接除以长度，
      是因为后者会把一条只有三个字、恰好完全命中的记忆推到不合理的高位。

    **一次外部往返都没有**，因此没有取消检查点——调用方在调它之前检查。
    """
    if not query_tokens or not documents:
        return tuple(0.0 for _ in documents)

    wanted = set(query_tokens)
    counters = [Counter(tokens) for tokens in documents]
    total = len(documents)
    # 文档频率只统计查询里出现过的词：其余词的 idf 永远乘在 0 上。
    document_frequency = Counter(
        token for counter in counters for token in wanted if counter[token]
    )

    scores: list[float] = []
    for counter, tokens in zip(counters, documents, strict=True):
        if not tokens:
            scores.append(0.0)
            continue
        accumulated = 0.0
        for token in wanted:
            occurrences = counter[token]
            if not occurrences:
                continue
            # +1 让「每条都提到」的词得到 idf≈0 而不是负数；分子的 +1 让单条记忆的
            # 情况下 idf 仍为正（否则 total==1 时一切都是 0 分）。
            idf = math.log((total + 1) / (document_frequency[token] + 1)) + 1.0
            accumulated += (1.0 + math.log(occurrences)) * idf
        scores.append(accumulated / math.sqrt(len(tokens)))
    return tuple(scores)


def rank(query: str, texts: Sequence[str], *, limit: int, min_score: float = 0.0) -> tuple[Scored, ...]:
    """把查询与候选文本变成按相关性降序的前 `limit` 条。

    **同分时按下标升序**（`sorted` 是稳定的，配上 `-value` 的键即可），因此调用方给的
    顺序在同分下被保留——`partition.RECALL_ORDER` 的「由窄到宽」正是靠这条生效。
    `min_score` 是一道闸门：一条一个词都没命中的记忆得 0 分，把它塞进上下文只是白占预算。
    """
    if limit <= 0 or not texts:
        return ()
    query_tokens = tokenize(query)
    documents = [tokenize(text[:_MAX_SCORED_CHARS]) for text in texts]
    values = score(query_tokens, documents)
    candidates = [
        Scored(index, value) for index, value in enumerate(values) if value > min_score
    ]
    candidates.sort(key=lambda item: -item.value)
    return tuple(candidates[:limit])
