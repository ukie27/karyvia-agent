# NucleaMind 模块化开发方案

- 状态：评审后修订
- 更新时间：2026-08-10
- 文档阶段：开发执行计划
- 上游依据：[`requirements-analysis.md`](./requirements-analysis.md)、[`technical-design.md`](./technical-design.md)

## 1. 文档定位

本文档把技术方案拆成 32 个可独立开发、独立验收的模块，回答三个问题：

1. 每个模块做什么、依赖谁、交付哪些文件。
2. 每个模块的验收标准是什么，怎么用命令验证。
3. 模块之间的推进顺序，以及每个阶段结束时系统处于什么可用状态。

模块编号 `D00`–`D31` 全局稳定，用于 commit message、分支名和进度跟踪。
编号不随顺序调整而变化。

## 2. 模块化开发的工作方式

### 2.1 单个模块的标准流程

`D00` 是唯一例外：它没有新逻辑可测，流程见该模块自身的 A0–A3。

```text
1  读技术方案对应小节，确认边界。有歧义先提出，不靠猜测实现
2  先写测试：至少覆盖该模块的正常路径 + 技术方案中点名的异常场景
3  实现，直到测试通过
4  .venv\Scripts\python.exe -m ruff check src/ plugins/
5  uv run --no-sync basedpyright
6  .venv\Scripts\python.exe -m pytest tests/architecture -q     # 架构守卫必过
7  .venv\Scripts\python.exe -m pytest <本模块测试> -q
8  按 §2.2 逐条核对验收清单
9  提交（单一意图，小步提交）
```

### 2.2 通用完成定义（DoD）

每个模块除自身验收项外，都必须满足以下 7 条：

| # | 条件 |
| --- | --- |
| 1 | `ruff check` 无新增告警 |
| 2 | `basedpyright` 严格模式通过，新增 `Any` 均带 `# boundary:` 注释 |
| 3 | `tests/architecture` 全部通过，无 skip、无 xfail（`D00` 除外，此时该目录尚不存在） |
| 4 | 新增模块首个 docstring 含「职责/不负责」两行 |
| 5 | 满足 §12.1 规模阈值：`kernel/` 单文件 ≤500 行、单函数 ≤60 行、圈复杂度 ≤12 |
| 6 | 该模块引入的每个错误码在 `contracts/errors.py` 注册且有测试触达 |
| 7 | 技术方案若因实现发现而需修订，先改技术方案再改代码 |

第 3 条是硬门禁：架构守卫失败不允许「先合并后修」。

### 2.3 阶段性可用状态

每个阶段结束时系统应达到的状态，这是判断「是否可以进入下一阶段」的依据：

| 阶段 | 结束时的可用状态 |
| --- | --- |
| 0 | 仓库为目标结构，现有功能不变；架构守卫对空骨架通过；CI 门禁生效 |
| 1 | 契约与注册表可用；覆盖解析的全部冲突分支有测试 |
| 2 | Turn Engine 可用 Fake Provider 完整跑通迭代、限额与取消 |
| 3 | 实例可加载配置、获取锁、发布与查询事件 |
| 4 | **第一个集成里程碑**：Fake 能力下端到端完成一次 turn |
| 5 | 七项内建能力齐备，各自通过契约测试 |
| 6 | **开箱可用里程碑**：只配凭据即可完成带工具调用的 turn（需求 §16.1） |
| 7 | **Plugin Runtime 里程碑**：外部插件可注册能力并覆盖内建（需求 §16.2） |
| 8 | `legacy/agent/` 与 `legacy/cli/` 已删除，剩余调用方改调新 Kernel |

阶段 4 的位置是刻意安排的：在写任何真实能力之前先用 Fake 打通整条路径，
把集成风险提前暴露，避免七个内建能力全部写完才发现编排层契约不合。

### 2.4 两处相对技术方案的顺序调整

**其一：重构提前到最前面（`D00`）。** 技术方案初稿曾约定「首版不引入 `nucleamind`
包名前缀，重命名等 Kernel 稳定后再做」。改为最先做，理由是重命名成本随代码量单调
上升，而此刻仍能把变更限制在结构、导入和明列的对外名称；遗留业务与配置行为可用
现有基线验证。等新架构写完再动，要改的就不止今天这 2979 个导入点了。
技术方案 §2.3、§13 M-A 已同步修订。

**其二：遗留 Agent 路径改为最后一次性删除（`D31`）。** 技术方案 §13 M2 原本把
「旧 `AgentLoop` 改为薄适配层」放在 turn 引擎拆分之后。现改为：内建能力与插件体系
齐备后直接删除 `legacy/agent/`，不搭桥、不设双路径开关。本项目是改造而非兼容发行版，
双路径开关要求两套实现长期共存、双份测试，成本远高于收益——回退用 git 即可。

两处调整合起来的效果：`D00` 之后，`legacy/` 内部代码在阶段 1–7 期间**完全不动**，
新 Kernel 在同一仓库内以独立入口 `nm` 并行生长。任何阶段中止都不影响现有可用功能。

## 3. 模块地图

```text
阶段 0  基座        D00 仓库重构与包重命名 ── D01 架构守卫与 CI 门禁
                     |
阶段 1  契约与注册   D02 契约·基础 ── D03 契约·领域 ── D04 契约·能力
                                              \            |
                                               D05 sdk 骨架与夹具
                                                            |
                                               D06 Capability Registry
                     |
阶段 2  Turn 内核    D07 旧实现行为基线 ── D08 取消与预算 ── D09 Turn Engine
                     |
阶段 3  支撑         D10 实例布局与配置 ── D11 Secret 与凭据
                                        D12 可观测性
                     |
阶段 4  编排闭环     D13 分流与并发 ── D14 Orchestrator ── D15 骨架集成验收 ★
                     |
阶段 5  内建能力     D16 内建加载路径与契约测试套件
                       ├─ D17 session_jsonl    ├─ D20 tools_fs
                       ├─ D18 context_basic    ├─ D21 tools_shell
                       ├─ D19 model_openai     └─ D22 commands_core
                       └─ D23 cli_entry + runtime/embed 组装 + nm 入口
                     |
阶段 6  开箱可用     D24 首次运行体验与开箱可用验收 ★
                     |
阶段 7  插件运行时   D25 Manifest 与发现 ── D26 资源门面与 PluginContext
                       ── D27 两阶段加载 ── D28 生命周期 ── D29 nm plugins 与诊断
                       ── D30 示例插件与 Plugin Runtime 验收 ★
                     |
阶段 8  遗留路径清理 D31 遗留 Agent 路径切换与删除
```

★ = 里程碑验收模块，不新增功能，只做集成验证。

`D17`–`D22` 之间无相互依赖，可并行开发；`D23` 需等 `D17`–`D22` 全部完成；
其余按箭头顺序串行。

## 4. 阶段 0：工程基座

阶段 0 有两个模块：`D00` 把仓库搬成目标结构，`D01` 立起守卫。顺序不可颠倒——
守卫要写在最终路径上，先重构再写守卫，只写一次。

### D00 仓库重构与包重命名

**依赖**：无。这是第一个模块，必须先落地。

**要点**（技术方案 §4.1–§4.5、§13 M-A）

这是**受限的结构与命名迁移**：允许且仅允许包名、发行名、导入路径和 CLI 名称发生
技术方案 §4.5 明列的变化；不改 Agent 业务逻辑，不改遗留配置格式、环境变量、状态目录，
也不拆遗留模块。验收标准是除明列命名变化外，现有行为基线逐项一致。

分四个可独立回退的 commit：

```text
A0  捕获行为基线（必须在任何改动之前）
    scripts/test_snapshot.py capture --out migration-snapshot/before.json
    产物提交入库，D00 验收通过后删除

A1  git mv nanobot/ src/nucleamind/legacy/
    用 git mv 保留历史，使 git log --follow 仍可追溯

A2  scripts/migrate_names.py 机械重写
    导入前缀   nanobot.        ->  nucleamind.legacy.       （约 2979 处）
    同时处理：字符串形式的模块路径、pytest testpaths、basedpyright include、
              构建资源路径和 docstring 中的模块引用
    不处理：  NANOBOT_*、~/.nanobot/、camelCase 配置别名等遗留运行契约

A3  pyproject.toml 重写
    name = "nucleamind"；packages = ["src/nucleamind"]
    [project.scripts] nm = "nucleamind.runtime.cli.main:app"     # 唯一命令
    [project.entry-points."nucleamind.plugins"] 组建立（暂空）
    hatch include / sdist / wheel 路径全部更新
    新增 runtime/legacy_entry.py，作为 nm legacy 到遗留 CLI 的唯一过渡适配器
```

**A0 不可省略。** D00 的验收标准是「与重构前逐项一致」，但「重构前」这个状态只存在于
动手之前的那一刻。不先落盘，D00 做完就无从比对，验收标准会退化成「看起来能跑」。
当前仓库有约 5850 个用例，其中可能本就存在失败项——基线要记录的是**完整的用例 ID
与结果集合**，不是「全绿」这个假设。

`scripts/test_snapshot.py` 的契约（D00 交付物，验收通过后与 `migration-snapshot/` 一并删除）：

```text
capture  --out <path>
  运行 pytest --collect-only 与完整测试，输出规范化 JSON：
    { "canonical_id": "<outcome>", ... } + 各 outcome 计数 + 采集错误列表
  采集错误（collection error）非空时以非零码退出并拒绝写出基线——
  模块导入失败会让用例静默消失，这种基线不可信

compare  --before <path> --after <path>
  输出三类差异并在非空时以非零码退出：
    missing        基线中有、当前没有   <- 最危险：用例被结构迁移弄丢了
    added          当前有、基线中没有
    outcome_changed 同一用例结果变化
```

**用例 ID 规范化规则**（`capture` 内实现，`compare` 直接比对规范化后的 ID）：
文件移动会改变 pytest node ID，因此按「根标签 + 相对路径」归一，而不是简单剥前缀：

| 原始路径前缀 | 根标签 | 说明 |
| --- | --- | --- |
| `src/nucleamind/legacy/` | `pkg` | 重构后的包内测试 |
| `nanobot/` | `pkg` | 重构前的包内测试 |
| `tests/legacy/` | `tests` | 重构后的独立测试 |
| `tests/` | `tests` | 重构前的独立测试 |

规范化结果形如 `pkg:channels/telegram/tests/test_x.py::test_y`。
保留根标签而不是全部剥成相对路径，是为了避免 `tests/channels/...` 与
`nanobot/channels/...` 归一后相撞。路径分隔符统一为 `/`，消除 Windows 与 Linux 差异。

**新层不写长期兼容垫片**（技术方案 §4.5）：不保留 `nanobot` 命令别名；后续新 Kernel
只读 `NUCLEAMIND_*`、`~/.nucleamind/instances/<instance>/` 和 snake_case 配置，不双读旧格式。
但 `legacy/` 在被删除前继续读取 `NANOBOT_*`、`~/.nanobot/` 和原 camelCase 配置，
以便 D00 能验证遗留功能没有被结构迁移破坏。

`legacy/` 内部代码在迁移期仍需可运行——这不是兼容层，而是尚未改写完的实现。
入口收敛为 `nm legacy <原 nanobot 参数>` 单个子命令：只有一个命令名，
旧功能在明确的临时命名空间下可达，`D31` 随 `legacy/agent/` 一并删除。
此时 `runtime/cli/main.py` 只是最小骨架，仅暴露 `nm legacy` 与 `nm --version`，
真正的子命令在 `D23` 落地。

`runtime/legacy_entry.py` 是 `R6` 的唯一迁移期例外，只转发参数、退出码和标准流。
它不得被其他模块导入，不得承载新功能；D01 用精确路径白名单约束，D31 连同白名单删除。

同时创建新层的空骨架（只有 `__init__.py` 与模块 docstring，无实现）：
`contracts/`、`kernel/`、`sdk/`、`builtins/`、`runtime/`、`embed/`，
以及顶层 `plugins/`、`examples/plugins/`、`deploy/`。空骨架让 `D01` 的守卫
有真实路径可断言，也让后续模块直接写在最终位置。

**交付**

```text
migration-snapshot/before.json    # A0 产物，D00 验收通过后删除
src/nucleamind/legacy/            # 原 nanobot/ 全部内容
src/nucleamind/{contracts,kernel,sdk,builtins,runtime,embed}/__init__.py
src/nucleamind/runtime/cli/main.py  # 最小入口：nm legacy + nm --version
src/nucleamind/runtime/legacy_entry.py # R6 唯一过渡适配器
src/nucleamind/legacy/README.md   # 隔离规则与迁移状态
plugins/  examples/plugins/  deploy/
tests/legacy/                     # 原 tests/ 中针对遗留代码的用例
scripts/test_snapshot.py          # 一次性工具，D00 验收通过后删除
scripts/migrate_names.py          # 一次性脚本，D00 完成后删除
scripts/legacy_debt.py            # 常驻：统计 legacy/ 文件数与行数
pyproject.toml                    # 全面重写
AGENTS.md                         # 更新包名、目录约定与「不兼容 nanobot」原则
docs/                             # 记录新层不读取旧实例目录；legacy 迁移期仍可读取
```

**验收**

```bash
# 1. 行为基线比对（硬门禁）
.venv\Scripts\python.exe scripts/test_snapshot.py capture --out migration-snapshot/after.json
.venv\Scripts\python.exe scripts/test_snapshot.py compare \
    --before migration-snapshot/before.json --after migration-snapshot/after.json

# 2. 打包与入口
.venv\Scripts\python.exe -m pip install -e .
nm --version && nm legacy --help                      # 唯一命令 + 遗留子命令可用
.venv\Scripts\python.exe -m build

# 3. 静态检查与债务基线
.venv\Scripts\python.exe -m ruff check src/
uv run --no-sync basedpyright
.venv\Scripts\python.exe scripts/legacy_debt.py       # 输出基线数字
```

- `compare` 必须报告 `missing` 与 `outcome_changed` 均为空。因导入路径变化而更新测试
  源码是允许的，但不得改变断言语义；`added` 非空需逐条说明来源（通常只应是新层空骨架
  带来的采集变化，若无则应为空）。
- 两次 capture 的采集错误列表均为空——有模块导入失败时用例会静默消失，
  此时用例数相符也不能作为通过依据。
- `nm legacy` 对原 `NANOBOT_*`、`~/.nanobot/` 和 camelCase 配置的读取行为保持不变。
- `python -m build` 产物包含 `templates/`、`skills/`、`web/dist/` 等非 Python 资源。
- 仓库根目录下不存在可被误导入的 `nucleamind/` 目录（`src/` 布局生效）。
- `git log --follow src/nucleamind/legacy/agent/loop.py` 能看到重构前的历史。
- 新层检索确认无意外旧名：
  `rg -i "nanobot" src/nucleamind --glob "!legacy/**"` 只允许命中迁移说明与
  `nm legacy`；`legacy/` 中的旧环境变量、配置键和历史叙述不计为遗漏。

验收通过后，在同一 PR 的收尾 commit 中删除 `migration-snapshot/`、`scripts/test_snapshot.py`
与 `scripts/migrate_names.py`——它们是 D00 专用设施，留在仓库里只会误导后续开发。

**规模**：两个一次性脚本约 400 行 + 配置重写。改动文件数极大，业务逻辑保持不变。
**风险**：中。风险在导入、构建资源和入口迁移遗漏，以及误把配置迁移混入本模块。
验收的行为基线比对、遗留配置回归和新层旧名扫描是三道互补防线；A0 是前两道的前提。

### D01 架构守卫与 CI 门禁

**依赖**：D00

**交付**

```text
tests/architecture/__init__.py
tests/architecture/test_import_boundaries.py     # R1–R6 的 AST 断言
tests/architecture/test_module_docstrings.py     # 「职责/不负责」两行检查
tests/architecture/test_file_size.py             # 单文件行数阈值
tests/architecture/test_any_usage.py             # Any 需带 # boundary: 注释
tests/architecture/test_legacy_debt.py           # legacy/ 指标只降不升
scripts/check_startup_cost.py                    # 启动开销记录脚本
pyproject.toml                                   # ruff 分层规则集
.github/workflows/ci.yml                         # 或现有 CI 配置扩展
```

**要点**（技术方案 §3.1、§12.1、§12.4）

- 边界检查用 `ast.parse` 遍历 `src/nucleamind/` 各层与 `plugins/`，
  收集 `Import` / `ImportFrom` 的模块名后断言 `R1`–`R6`。
- `R6` 是**单向**规则：断言新层不 import `legacy/`，但允许 `legacy/` import 新层。
  实现时不要写成对称检查。唯一例外是精确文件
  `src/nucleamind/runtime/legacy_entry.py`，并额外断言没有第二个例外。
- 新层此时只有空骨架，检查必须对**空目录返回通过**而不是报错，
  否则 `D01` 自身无法验收。同时要有「注入违规样例必须失败」的反向测试，
  证明守卫真的会拦。
- ruff 规则分层：新层加 `C901`、`PLR0915`、`TRY`、`ASYNC`；
  用 `[tool.ruff.lint.per-file-ignores]` 让 `src/nucleamind/legacy/` 保留原规则集。
- 沿用既有约定：**不引入 `ruff format`**。

**验收**

```bash
.venv\Scripts\python.exe -m pytest tests/architecture -q           # 全绿
.venv\Scripts\python.exe -m pytest tests/architecture -q -k inject # 反向样例被拦
.venv\Scripts\python.exe -m ruff check src/ plugins/               # 无新增告警
```

- 反向测试证明 `R1`–`R6` 各自都能拦住一个构造的违规样例，共 6 个反向用例。
- `R6` 的反向测试需覆盖：普通新层 import legacy 必须失败；legacy import 新层必须通过；
  唯一适配器 import legacy 必须通过；第二个白名单外适配器必须失败。
- `legacy/` 债务指标接入 CI，数字只允许下降。
- CI 中架构守卫是独立阶段，失败即阻断，不允许 skip / xfail。

**规模**：约 500 行测试 + 配置。**风险**：低。

## 5. 阶段 1：契约与注册表

### D02 契约·基础层

**依赖**：D01

**交付**：`contracts/__init__.py`、`ids.py`、`errors.py`、`events.py`

**要点**（技术方案 §5.1、§5.2）

- `SessionKey` 是结构化 dataclass，提供 `storage_id()` 且分隔符转义可逆——
  这是 `EDG-203` 的根，必须有「不同输入不可能产生同一 storage_id」的测试。
- `NucleaError` 构造时即完成 `detail` 脱敏，不依赖日志层。
- `ErrorCategory` 11 个取值全部落地；错误码常量集中在一处，禁止散落字面量。
- `RuntimeEvent` 带 `Correlation` 与单调 `sequence`。

**验收**

- `storage_id()` 往返与冲突测试：`("a","b:c")` 与 `("a:b","c")` 必须产出不同 id。
- `NucleaError` 携带哨兵密钥时，`user_message`、`detail`、`repr` 均不含哨兵值。
- `contracts/` 不 import 任何本项目模块（由 D01 守卫自动覆盖）。

**规模**：约 350 行。**风险**：低，但 `SessionKey` 编码一旦发布即为持久化契约，需评审。

### D03 契约·领域与执行层

**依赖**：D02

**交付**：`contracts/message.py`、`session.py`、`context.py`、`tool.py`、`model.py`

**要点**（技术方案 §5.2，需求 §10.2–§10.6）

- `InboundMessage` / `OutboundMessage` 按需求 §10.2、§10.3 逐字段落地；
  `OutboundMessage` 必须自带 `channel_id + conversation_id + turn_id`（`MSG-006`）。
- `metadata` 类型为 `Mapping[str, JsonValue]` 并有大小上限校验。
- `ToolResult.side_effect` 是必填三态枚举，不给默认值——迫使每个构造点显式表态。
- `ContextFragment.trust` 四级枚举齐全，是 `CMD-005`、`EDG-306` 的落点。
- 全部 frozen dataclass；`JsonValue` 递归类型别名统一定义在 `contracts/__init__.py`。

**验收**

- 每个契约类型有构造 + 不可变性测试（尝试赋值抛 `FrozenInstanceError`）。
- 字段追溯表：在测试或 docstring 中标明每个字段对应需求 §10 的哪一行。
- `metadata` 超限、附件缺失且内容为空等校验场景各有测试（需求 §10.2 校验规则）。

**规模**：约 600 行。**风险**：中，字段遗漏会在阶段 5 才暴露，因此验收要求逐字段追溯。

### D04 契约·能力层

**依赖**：D03

**交付**：`contracts/capability.py`、`contracts/command.py`、`contracts/protocols.py`

> `command.py` 是实施时补的第三个文件：`CommandHandler` 的输入输出（`CommandSpec` /
> `CommandInvocation` / `CommandResult`）不定型，Protocol 就无法类型化。同理，`HookHandler`
> 需要的 `HookContext` / `HookOutcome` 落在 `capability.py`。规模因此由 450 行增至约 1030 行。

**要点**（技术方案 §5.1、§6.1）

- `CapabilityKind` 9 个取值 + 每个 kind 的 arity 常量表（MULTI / MULTI-unique / SINGLETON）。
- `ProviderId` 为 `Builtin() | Plugin(id)` 的联合类型，不用裸字符串。
- `protocols.py` 是 Kernel 唯一依赖面，包含 `ModelProvider`、`ToolHandler`、
  `ContextProvider`、`SessionStore`、`MemoryProvider`、`Channel`、`CommandHandler`、
  `HookHandler`。每个方法 docstring 必须写明异常约定与取消语义。
- Protocol 不含实现；`runtime_checkable` 只用于诊断输出，不用于控制流。

**验收**

- arity 表与技术方案 §6.1 的表格逐行一致（用测试断言，避免文档漂移）。
- 每个 Protocol 有一个最小 Fake 实现通过 `isinstance` 结构检查。
- Protocol 方法数量快照测试：新增方法必须显式改快照，落实 `NFR-104`。

**规模**：约 450 行。**风险**：中，Protocol 粒度过粗或过细都难改，需评审确认。

### D05 sdk 骨架与测试夹具

**依赖**：D04

**交付**

```text
src/nucleamind/sdk/__init__.py       # __all__ 规范性清单
src/nucleamind/sdk/api.py            # NucleaAPI Protocol（9 方法）
src/nucleamind/sdk/manifest.py       # PluginManifest / CapabilityDecl
src/nucleamind/sdk/version.py        # SDK_VERSION
src/nucleamind/sdk/testing/fakes.py  # Fake 实现
src/nucleamind/sdk/testing/contracts.py  # 5 个契约测试基类骨架
tests/sdk/test_public_surface.py
```

**要点**（技术方案 §7.2、§7.5、§7.6）

- `NucleaAPI` 恰好 9 个注册方法 + `ctx`，其中包含可覆盖内建 CLI 的
  `register_cli_entry()`。
- `testing.py` 第一版提供 `FakeModelProvider`（可脚本化返回 tool_call 序列）、
  `InMemorySessionStore`、`RecordingHook`，以及 5 个契约测试基类的空骨架。
  Fake 必须在公开 SDK 内，因为它同时是插件开发者的验收工具。
- `manifest.py` 只做数据与校验，**导入它不得产生任何副作用**（技术方案 §7.2 硬约束）。

**验收**

- `test_public_surface.py` 建立 `__all__` 快照；手动增删一个导出会让测试失败。
- `NucleaAPI` 方法数断言为 9，并断言 `register_cli_entry` 存在。
- 导入 `sdk.manifest` 的耗时与副作用测试：无网络、无文件写入。
- `sdk/` 只 import `contracts/`（D01 守卫覆盖）。

**规模**：约 500 行。**风险**：中，SDK 表面是长期兼容承诺的起点，必须评审后再冻结。

### D06 Capability Registry 与覆盖解析

**依赖**：D05

**交付**：`kernel/registry/capability.py`、`kernel/registry/resolution.py`、
`tests/kernel/test_registry.py`、`tests/kernel/test_resolution.py`

**要点**（技术方案 §6.1）

- `RegistrationBatch`：注册先入暂存区，`commit()` 才并入 registry，`rollback()` 整体丢弃。
  这是 `EDG-103` 的落地手段，必须在 registry 层实现，而不是在加载器里补救。
- 覆盖只认 manifest 的显式 `overrides`，永不由注册顺序决定。
- 冻结机制：解析完成后 registry 不可写，写入尝试抛 `KERNEL_INTERNAL`。
- 内部索引 `dict[(kind, name)]`，运行期查找 O(1)，无扫描（`NFR-403`）。

**验收** —— 必须覆盖以下全部冲突分支，每条一个测试：

| 场景 | 期望 |
| --- | --- |
| 同 kind 同 name 重复注册且无 overrides | 启动错误 |
| `overrides` 目标不存在 | `capability.override_target_missing`，不降级为新增 |
| 两个插件覆盖同一目标 | `capability.override_conflict` |
| SINGLETON kind 注册两个实现 | 启动错误 |
| CONTEXT / HOOK 同名并存 | 全部生效，按 `(priority, provider)` 排序 |
| 覆盖成功 | 被覆盖项进入 `shadowed`，报告中可见 |
| 批次中途抛异常 | registry 无残留，`rollback` 后状态与批次开始前一致 |
| 冻结后写入 | 抛 `KERNEL_INTERNAL` |

- `ResolutionReport` 可序列化为 JSON，字段 `active/shadowed/disabled/failures` 齐全。
- 排序稳定性测试：同 priority 时按 provider id 字典序，多次运行结果一致。

**规模**：约 450 行实现 + 500 行测试。**风险**：中高。这是全项目冲突语义的唯一来源，
分支遗漏会在阶段 7 变成难查的行为不确定。验收要求分支表逐条对齐。

## 6. 阶段 2：Turn 内核

### D07 旧实现行为基线

**依赖**：D01（不依赖新契约，可与阶段 1 并行）

**交付**：`tests/baseline/test_loop_behavior.py`、`tests/baseline/test_runner_behavior.py`、
`tests/baseline/README.md`（说明基线用途与删除时机）

**要点**（技术方案 §13 M2）

针对 `legacy/agent/loop.py`、`runner.py` 编写并通过，锁定以下行为：

- 迭代上限触发后的终止方式与返回内容。
- 工具执行失败、超时、参数非法时模型收到什么。
- 流式增量的聚合顺序与最终内容一致性。
- 并发/串行工具调度的实际顺序。
- 工具结果超长时的截断行为。

**验收**

- 全部基线测试在**旧实现**上通过，且不依赖真实网络（用现有测试夹具或录制响应）。
- `README.md` 明确：这些测试在 `D09`、`D14` 用于比对新实现，在 `D31` 删除 `legacy/agent/`
  的同一个 PR 内一并删除。

**规模**：约 500 行测试。**风险**：低，但这是 M2 不失控的前提，不能跳过。

### D08 取消与预算

**依赖**：D04

**交付**：`kernel/turn/cancel.py`、`kernel/turn/limits.py`、`tests/kernel/test_cancel.py`、
`tests/kernel/test_limits.py`

**要点**（技术方案 §6.4）

- `CancelToken.request()` 幂等，重复调用不产生新状态（`EDG-206`）。
- `child()` 派生 token 用于工具与子 turn，父取消传播到子。
- 显式 `CancelToken` 而非依赖 `asyncio.CancelledError`——这是「保存已产生内容并标记取消」
  语义可实现的前提，必须有测试证明取消后数据仍可保存。
- `TurnLimits` 6 项预算全部有保守默认值。

**验收**

- 6 项预算逐项测试：达到上限的行为、可配置性、默认值存在性。
- 「缺省配置下不存在无界执行路径」的显式测试：构造一个永远返回 tool_call 的 Fake 模型，
  断言 turn 在有限步内以 `STOPPED_BY_LIMIT` 终止。
- 重复 `request()` 的幂等性测试。
- 父子 token 传播测试。

**规模**：约 300 行实现 + 350 行测试。**风险**：低。

### D09 Turn Engine

**依赖**：D06、D08，参考 D07

**交付**：`kernel/turn/engine.py`、`tests/kernel/test_engine.py`

**要点**（技术方案 §6.2）

- 目标 **≤400 行**，只通过 `EngineDeps` 与外界交互，无任何文件、网络、数据库操作。
- `deps` 回调抛出的异常全部转为 `TurnFailed` 事件，engine 自身不向上抛——
  避免出现「没有正常事件序列的中断」。
- 检查点 2、3、5、6 在 engine 内（1、4 在 orchestrator 侧的边界上）。
- 工具调度：`EXCLUSIVE` 串行，`PARALLEL` 并发，事件按完成顺序发出。

**验收**

- D07 基线测试中的**行为断言部分**在 engine + Fake Provider 下同样通过。
- engine 内 4 个检查点各有独立测试，断言中断后的 `side_effect` 标记正确。
- `deps` 每个回调各注入一次异常，断言产出 `TurnFailed` 而非异常穿透。
- 四个终态 `COMPLETED / CANCELLED / FAILED / STOPPED_BY_LIMIT` 各有测试。
- 行数检查通过（D01 守卫）；无 IO 由架构测试断言 import 清单。
- 全部测试不需要文件系统或网络，单次运行 ≤2 秒。

**规模**：约 400 行实现 + 600 行测试。**风险**：高。这是全项目最容易膨胀的文件，
一旦超过 400 行说明职责已经越界，应把新增逻辑移到 orchestrator。

## 7. 阶段 3：支撑设施

### D10 实例布局与配置加载

**依赖**：D02

**交付**：`kernel/config/layout.py`、`kernel/config/loader.py`、`tests/kernel/test_config.py`、
`tests/kernel/test_layout.py`

**要点**（技术方案 §6.7、§11）

- 三层配置：内置默认 → `config.json` → 环境变量白名单 →（测试用）进程参数。
  每个生效值可追溯来源，诊断中可见。
- 顶层 schema `extra="forbid"`，错误信息带 JSON Pointer 位置。
- `instance.lock` 用 `O_EXCL` + PID 存活检测；陈旧锁自动清理并记录。
- 配置损坏时**拒绝启动且不改写原文件**——这是 `EDG-501` 的核心，不是可选行为。
- 配置键只用 snake_case，对外 JSON 同形，不提供 camelCase 别名（技术方案 §4.5）。

**验收**

- 未知字段 / 类型错误 / 缺必填项各产生带位置的错误（`CFG-001`）。
- 配置损坏后原文件字节级不变（读前读后哈希一致）。
- 双进程抢锁：第二个报明确错误并列出占用 PID（`EDG-507`）。
- 陈旧锁（写入不存在的 PID）被清理且记录事件。
- Windows 与 Linux 均通过（CI 矩阵）。

**规模**：约 450 行实现 + 400 行测试。**风险**：中，跨平台文件锁差异需实测。

### D11 Secret 与凭据

**依赖**：D10

**交付**：`kernel/config/secrets.py`、`tests/kernel/test_secrets.py`

**要点**（技术方案 §6.7）

- `${VAR}` 引用解析为 `SecretStr`，`__repr__` / `__str__` / 序列化恒为 `***`。
- 配置写回时保留原始 `${VAR}` 字面量，绝不回写明文（`CFG-003`）。
- 缺失变量只报变量名，不报值（`EDG-502`）。

**验收**

- 哨兵测试：把哨兵值放进环境变量，断言它不出现在任何序列化输出、日志、`repr` 中。
- 写回往返测试：读取 → 修改其他字段 → 写回，`${VAR}` 字面量原样保留。
- 缺失变量的错误消息包含变量名、不包含任何值。

**规模**：约 200 行实现 + 250 行测试。**风险**：低，但哨兵测试必须覆盖全部输出路径。

### D12 可观测性

**依赖**：D02

**交付**：`kernel/observability/bus.py`、`redaction.py`、`diagnostics.py`、
`tests/observability/`

**要点**（技术方案 §6.8）

- `EventBus` 只做扇出，不认识具体消费者。内建两个 sink：JSONL 文件 + 有界内存环。
- **脱敏在事件构造时完成**，不依赖 sink——否则新增 sink 就会绕过脱敏。
- 订阅者异常与超时被隔离，不影响 turn（`NFR-204`）。
- `diagnostics.py` 三个只读查询：`capabilities()`、`plugins()`、`turn(turn_id)`。

**验收**

- 单 turn 的事件序列可按 `sequence` 完整重放（`OBS-002`）。
- 哨兵扫描：埋入密钥后扫描 JSONL sink 与内存环，无泄漏（`OBS-003`）。
- 抛异常的订阅者不影响其他订阅者，也不影响事件发布返回。
- 内存环容量上限生效，超出后丢弃最旧而非无限增长（`NFR-404`）。

**规模**：约 400 行实现 + 350 行测试。**风险**：低。

## 8. 阶段 4：编排闭环

### D13 输入分流与 Session 并发

**依赖**：D06、D10

**交付**：`kernel/routing/dispatcher.py`、`kernel/routing/session_lock.py`、
`kernel/routing/dedup.py`、`tests/kernel/test_dispatcher.py`、
`tests/kernel/test_session_lock.py`、`tests/kernel/test_dedup.py`

> `dedup.py` 在技术方案 §4.2 的目录树里本来就有（原拼作 `dedupe.py`），本清单初版漏列，
> 落地时补回：去重是准入判定，与「谁能写这个 session」无关，并进 `session_lock.py` 会让
> 那个文件同时管两件不相干的事。

**要点**（技术方案 §6.3、§6.5）

- 四种 `Disposition`；命令名冲突在启动期报错而非调用期择一（`CMD-002`）。
- 命令即使不进模型也分配 `turn_id` 并发布事件（`KER-010`）。
- 三种并发策略共用一条不变量：同 session 写入只经过持锁的单一写者。
- 去重 LRU：`(channel_id, message_id)`，默认 4096 条 / 10 分钟。

**验收**

- 三种策略（queue / merge / reject）各有测试；并发 20 条消息断言历史顺序严格 FIFO。
- 命令执行抛异常时：返回可诊断错误、会话仍可用、进程不退出（`CMD-003`）。
- 重复投递同一 `message_id` 不触发第二次工具执行（`EDG-201`）。
- 队列满时按策略降级为 reject 并返回明确错误，不静默丢弃。

**规模**：约 400 行实现 + 450 行测试。**风险**：中，并发测试需注意确定性，
避免用 sleep 制造时序，改用显式事件同步。

### D14 Turn Orchestrator

**依赖**：D09、D12、D13

**交付**：`kernel/turn/orchestrator.py`、`kernel/turn/hooks.py`、`tests/kernel/test_orchestrator.py`

**要点**（技术方案 §6.2、§6.6、§10.2）

- 目标 **≤500 行**。负责 session 加载写入、context 组装调度、事件发布、出站消息生成。
- Hook 分两类：Observer 并发只读失败隔离；Interceptor 顺序执行按
  `(priority, plugin_id)`，可改流水线。当前 9 个 Hook 全部接入。
- Context 组装完整实现技术方案 §10.2 第 7 步的 a–e 五个子步骤，
  包括按 `trust` 决定放置位置、`UNTRUSTED` 包裹为带前缀的数据块。
- 检查点 1、4 在此实现。

**验收**

- 技术方案 §10.2 的 14 步流程逐步可追踪（用事件序列断言）。
- Context 裁剪：SYSTEM 不被裁剪；超预算时按 priority 逆序丢弃；HISTORY 从最旧丢
  （`CTX-003`、`EDG-301`）。
- `UNTRUSTED` 片段被包裹且带固定前缀，无法出现在系统指令位置（`CMD-005`、`EDG-306`）。
- Context Provider 超时：跳过并记录（`CTX-005`、`EDG-302`）。
- Interceptor 顺序确定性测试：多次运行顺序一致。
- Observer 抛异常不影响 turn 结果（`NFR-204`）。
- 持久化失败 → turn `FAILED`，不伪装成功（`SES-003`）。

**规模**：约 500 行实现 + 700 行测试。**风险**：高。这是第二个容易膨胀的文件，
Hook 与 Context 逻辑应尽量下沉到 `hooks.py` 和 Provider 侧。

### D15 骨架集成验收 ★

**依赖**：D14

**交付**：`tests/integration/test_skeleton_turn.py`、`docs/project/` 中的阶段小结

**要点**：不写新功能，只用 `sdk.testing` 的 Fake 能力把整条路径跑通。

**验收**

- Fake Model + Fake Tool + InMemorySessionStore 下完成一次含工具调用的完整 turn。
- 中断路径：turn 中途取消，断言终态 `CANCELLED`、已产生内容已保存、
  未执行工具 `side_effect=NONE`、会话仍可继续。
- 事件序列完整且可重放。
- 整个集成测试不触碰真实网络，运行 ≤5 秒。
- **阶段小结**：记录实现过程中发现的技术方案偏差，同步修订技术方案。

**规模**：约 300 行测试。**风险**：低，但这是最重要的一次早期风险暴露点。
若此处发现契约不合，修改成本远低于阶段 5 之后。

## 9. 阶段 5：内建默认能力

### D16 内建加载路径与契约测试套件

**依赖**：D15

**交付**

```text
src/nucleamind/builtins/registry.py       # BUILTIN_MANIFESTS 静态清单（此时为空元组）
src/nucleamind/kernel/plugins/host.py      # NucleaAPI 宿主实现：统一注册到 RegistrationBatch
src/nucleamind/kernel/plugins/builtin_loader.py   # 静态内建清单 bootstrap
src/nucleamind/runtime/wiring.py          # 组装根：registry ← builtins（R5 的唯一落点）
src/nucleamind/sdk/testing/contracts.py   # 补全 5 个契约测试基类
tests/architecture/test_builtin_no_privilege.py
tests/architecture/test_kernel_runs_without_builtins.py
```

**要点**（技术方案 §6.1 末段、§8、§12.3）

- 本模块先落地唯一的 Host `NucleaAPI` 实现。内建 bootstrap 和 D27 的外部插件 loader
  都通过它把能力写入 `RegistrationBatch`；**不允许存在内建专用注册 API**
  （`SDK-007`）。
- D16 的 `host.py` 只实现注册分派，并接收外部注入的 `PluginContext`；本阶段测试使用
  `FakePluginContext`。生产级 Context 由 D26 补齐，D26 不得重写注册分派。
- 内建与外部插件可以有不同的来源发现和生命周期编排：内建来自静态可信清单，外部插件
  还需 entry point、依赖和版本校验。差异不得延伸到能力注册接口。
- 契约测试基类：`ModelProviderContract`、`SessionStoreContract`、`ContextProviderContract`、
  `ToolContract`、`ChannelContract`。子类只需提供构造夹具即获得全部用例。
- 契约测试放在公开 SDK 内，因为它同时是插件开发者的验收工具。

**验收**

- `builtins/` 不 import `nucleamind.kernel.*`（架构测试断言，`BAS-005`）。
- 用同一个 Host API 分别注册一个 Fake builtin 和 Fake plugin，断言 registry 结果结构一致，
  只允许 `ProviderId` 不同。
- 禁用全部可禁用内建实现后，Kernel 用 Fake 能力仍能跑通一次 turn（`NFR-701`）。
- 5 个契约基类各自有一个「故意不合规的 Fake」能被测出来的反向样例。

**规模**：约 350 行实现 + 600 行契约基类。**风险**：中，契约基类的用例质量决定
后续所有实现的可替换性是否真的成立，需评审用例覆盖面。

### D17 内建 Session：session_jsonl

**依赖**：D16

**交付**：`builtins/session_jsonl/`、`tests/builtins/test_session_jsonl.py`、
`docs/` 中的存储格式说明

**要点**（技术方案 §8.1）

- 每 session 一个 JSONL（追加写）+ 一个 `meta.json`（原子替换）。
- 原子写复用 nanobot `agent/memory.py` 已验证的临时文件 + `fsync` + `os.replace`。
- 格式必须文档化且可被外部实现读取（`SES-006`），文档与实现同一模块交付。

**验收**

- 通过 `SessionStoreContract` 全部用例。
- 跨进程重启后历史可完整恢复（`SES-006`）。
- 半写模拟：在写入中途 kill 进程，重启后文件可解析、无半条记录（`EDG-504`）。
- 删除、过期、压缩的数据保留语义各有测试（`SES-005`）。
- 格式说明文档中的示例可被测试直接解析（防文档漂移）。

**规模**：约 350 行 + 300 行测试。**风险**：中，格式一旦发布即为迁移契约。

### D18 内建 Context：context_basic

**依赖**：D16

**交付**：`builtins/context_basic/`、`tests/builtins/test_context_basic.py`

**要点**（技术方案 §8.1）

- 系统指令 + 会话历史 + 按 token 预算的尾部保留裁剪。
- 无 Memory、无检索插件时必须产出可用上下文（`CTX-006`、`EDG-307`）。
- Context Provider **只读不写**，不得持久化任何内容（技术方案 §14 的职责划分风险项）。

**验收**

- 通过 `ContextProviderContract` 全部用例。
- 无 Memory 插件时组装正常完成，不产生缺失依赖错误（`EDG-307`）。
- Provider 没有写入路径：架构测试断言其不 import 任何持久化模块。
- token 估算与实际裁剪结果一致性测试。

**规模**：约 250 行 + 250 行测试。**风险**：低。

### D19 内建 Model：model_openai

**依赖**：D16

**交付**：`builtins/model_openai/`、`tests/builtins/test_model_openai.py`

**要点**（技术方案 §8.1、§15 第 5 项）

- OpenAI 兼容 Chat Completions；覆盖 OpenAI、Azure、vLLM/Ollama/LM Studio 与常见中转。
- 能力声明必须显式列出不支持项（如扩展 thinking），**不做静默降级**（`MOD-005`）。
- 限流、超时、认证失败、内容过滤分别映射到 `ErrorCategory` 的对应取值（`MOD-003`）。
- Provider 私有响应对象不越过边界（`10.6` 末段）。

**验收**

- 通过 `ModelProviderContract` 全部用例。
- 四类错误各有测试，映射到正确的 `ErrorCategory` 且 `retryable` 标记正确。
- 认证信息不出现在日志与事件中（哨兵测试，`MOD-002`）。
- 测试使用录制响应或本地 stub server，**不依赖真实网络**。
- 流式中途失败时不把部分输出标记为完整答案（`EDG-304`）。

**规模**：约 500 行 + 450 行测试。**风险**：中，流式与 tool_call 增量解析易出边界问题。

### D20 内建工具：tools_fs

**依赖**：D16

**交付**：`builtins/tools_fs/`（`fs.read` / `fs.write` / `fs.edit` / `fs.list` / `fs.grep`）、
`tests/builtins/test_tools_fs.py`

**要点**（技术方案 §8.2、§8.3）

- 复用 nanobot `agent/tools/path_utils.py` 的路径守卫，`realpath` 后重新校验。
- 每个工具可按名字单独禁用；模型可见列表与实际可执行集合同源（`TOL-006`）。
- 结果超限截断并置 `truncated=True`（`TOL-003`）。

**验收**

- 通过 `ToolContract` 全部用例（每个工具一次）。
- Workspace 逃逸测试矩阵：符号链接、`..`、Windows 大小写、重解析点（`EDG-405`），
  Windows 与 Linux 各跑一遍。
- 单工具禁用后，模型可见列表中同步消失（`TOL-006`）。
- 空文件、超大文件、二进制、损坏编码各有可预期结果（`EDG-205`）。
- 跨平台行为契约一致（`NFR-605`）：同参数产生同退出语义与同截断规则。

**规模**：约 600 行 + 550 行测试。**风险**：中高，路径安全测试矩阵必须完整，
这是 `NFR-302` 的唯一防线。

### D21 内建工具：tools_shell

**依赖**：D16

**交付**：`builtins/tools_shell/`（`shell.exec`）、`tests/builtins/test_tools_shell.py`

**要点**（技术方案 §8.3）

- 默认 cwd 限定 workspace，默认不继承敏感环境变量，可选进程级沙箱后端。
- Windows 与 Linux 分别实现命令构造，但**对外行为契约一致**。
- 取消支持：收到 token 后 grace 2000 ms，超时标记 `side_effect=UNKNOWN`（`EDG-407`）。

**验收**

- 通过 `ToolContract` 用例。
- 取消测试：可取消进程正常终止；故意不响应的进程在 grace 后标记 `UNKNOWN`。
- 敏感环境变量（含哨兵）不传入子进程。
- 跨平台契约测试：退出码语义、输出截断、超时行为在两平台一致（`EDG-404`、`NFR-605`）。
- 默认执行环境保守：子进程不继承敏感环境变量，扩大范围必须显式配置（`NFR-307`）。

**规模**：约 400 行 + 450 行测试。**风险**：高，跨平台进程管理与取消是经典难点。

### D22 内建命令：commands_core

**依赖**：D16、D13

**交付**：`builtins/commands_core/`、`tests/builtins/test_commands_core.py`

**要点**（技术方案 §8.1）

命令集：`/help`、`/config`、`/session`、`/plugins`、`/capabilities`、`/cancel`。

- `/capabilities` 与 `/plugins` 直接输出 `diagnostics.py` 的查询结果，
  标明每项能力由内建还是哪个插件提供（`PLG-006`、`NFR-502`）。
- `/config` 输出必须脱敏，不显示凭据值。

**验收**

- 6 个命令各有测试；`/capabilities` 在无插件时列出全部内建能力及提供方（§16.1 第 2 条）。
- `/config` 输出哨兵扫描无泄漏。
- 命令执行失败返回可诊断错误且会话可用（`CMD-003`）。
- 命令声明的名称、参数形式、说明和操作员限制可被 registry 统一列出（`CMD-001`）。

**规模**：约 400 行 + 350 行测试。**风险**：低。

### D23 内建 CLI 能力、runtime/embed 组装与 nm 入口

**依赖**：D17–D22、D14

**交付**

```text
src/nucleamind/builtins/cli_entry/   # 内建 CLI 能力：stdin/stdout ↔ 消息契约
src/nucleamind/runtime/cli/main.py   # nm 可执行程序：argv 解析与进程入口
src/nucleamind/runtime/cli/commands/ # nm run / config / session（plugins 见 D29）
src/nucleamind/runtime/bootstrap.py  # 启动序列（§10.1 的 10 步）
src/nucleamind/runtime/instance.py   # AgentInstance：就绪 / 运行 / 停止
src/nucleamind/embed/__init__.py     # 嵌入式 Python SDK 薄门面
tests/builtins/test_cli_entry.py
tests/runtime/test_bootstrap.py
tests/embed/test_embed.py
```

**要点**（技术方案 §4.2、§8.1、§10.1，需求 `BAS-009`、`BAS-010`、`EDG-108`）

`builtins/cli_entry/` 与 `runtime/cli/` 是两件事，不要合并：前者是可被插件覆盖的
**能力**（把 stdin 变成 `InboundMessage`），后者是不可被覆盖的**进程入口**
（解析 argv、组装实例）。`BAS-010` 说的「插件可覆盖 CLI 实现但失败时回落」
指的是前者；后者始终由 `runtime/` 拥有。

- 单次执行模式 + 交互式会话模式。
- 输入输出走统一消息契约，**不得有绕过 `InboundMessage` 的专用路径**（`MSG-007`）。
- `Ctrl-C` → `cancel.request()`；第二次 `Ctrl-C` 退出进程。
- CLI 不可禁用；配置试图禁用时显式拒绝（`EDG-108`）。
- 覆盖失败时回落到内建实现，且该项不允许配置为 `fail_start`（`BAS-010`）。
- `runtime/wiring.py` 是唯一同时 import `kernel/` 与 `builtins/` 的模块（`R5`）。
- `embed/` 只包装 `runtime/instance.py` 的稳定调用面，不 import `builtins/`，不复制一套
  turn 编排逻辑。

**验收**

- 交互式 turn 可中断，中断后会话可继续（§16.1 第 4 条）。
- 禁用 CLI 的配置被显式拒绝并说明原因（`EDG-108`）。
- 尝试禁用全部可禁用内建能力的配置下 CLI 仍可用（§16.1 第 5 条）。
- CLI 消息经过与其他 Channel 相同的契约路径（用 `ChannelContract` 验证）。
- 启动序列的 10 步可通过事件序列逐步追踪。
- 架构测试：除 `runtime/` 外无任何模块同时 import `kernel/` 与 `builtins/`。
- `embed.run()` 与 CLI 使用同一个 `AgentInstance`，同一 Fake 输入产生等价的 turn 结果。

**规模**：约 700 行 + 500 行测试。**风险**：中，终端信号处理在 Windows 与 Linux 有差异。

## 10. 阶段 6：开箱可用里程碑

### D24 首次运行体验与开箱可用验收 ★

**依赖**：D17–D23 全部完成

**交付**：`kernel/config/bootstrap.py`（首次运行初始化）、
`tests/e2e/test_out_of_box.py`、`scripts/check_startup_cost.py` 接入 CI

**要点**（技术方案 §10.1，需求 §16.1）

- 首次运行无配置文件时生成最小可用 `config.json`（含 `$schema` 与占位字段）
  并输出「填哪个文件、哪个字段」的指引（`EDG-506`、`BAS-006`）。
- 缺模型凭据的错误必须可操作：指出配置位置与字段名，不输出凭据值。

**验收** —— 逐条对应需求 §16.1 的五个里程碑条件：

| # | 验收项 | 方式 |
| --- | --- | --- |
| 1 | 全新环境只配凭据即完成一次带工具调用的 turn | `tests/e2e`，用录制响应，临时 HOME |
| 2 | 无插件时 `/capabilities` 列出全部内建能力及提供方 | e2e 断言输出内容 |
| 3 | 缺凭据时错误指向文件与字段名且不泄露值 | 哨兵扫描 |
| 4 | 交互式入口可中断，中断后会话可继续 | 模拟信号的 e2e 用例 |
| 5 | 禁用全部可禁用内建能力后 CLI 仍可用 | 配置矩阵测试 |

另加：

- 启动开销记录：无插件冷启动到可接受输入 ≤300 ms（不含解释器启动）。
  超出基线 20% 触发 CI 告警而非失败（`NFR-405`）。
- Windows 与 Linux 均通过。
- 端到端测试全程不依赖真实网络（`NFR-705`、`DST-003`）。

**规模**：约 250 行实现 + 500 行 e2e 测试。**风险**：中。
此模块通过即达成需求 §16.1，是对外可宣称「可用」的第一个节点。

## 11. 阶段 7：Plugin Runtime

### D25 Manifest 与插件发现

**依赖**：D24

**交付**：`kernel/plugins/manifest.py`、`kernel/plugins/discovery.py`、
`tests/plugins/test_manifest.py`、`tests/plugins/test_discovery.py`

**要点**（技术方案 §7.1、§7.2）

- 两条来源：entry point 组 `nucleamind.plugins` + 配置显式路径 `plugins.paths`。
- **不做 site-packages 全量扫描**，不做目录自动加载。
- 发现与启用分离：只有 `plugins.enabled` 列出的插件才导入其模块。
- 缺少 `sdk_range` 等兼容字段直接判定校验失败并列出字段路径（借鉴 OpenClaw
  `plugin-package-contract`），不做兜底猜测。

**验收**

- manifest 校验：缺必填字段、id 格式非法、版本非 PEP 440、平台不匹配各有测试并给出字段路径。
- 「导入 manifest 模块无副作用」测试：无网络、无文件写入、耗时低于阈值。
- 未启用的插件不被导入（用导入计数或 `sys.modules` 断言）。
- 启动开销回归：注册 20 个未启用插件后启动耗时无显著上升（`NFR-401`）。

**规模**：约 450 行 + 450 行测试。**风险**：中。

### D26 资源门面与 PluginContext

**依赖**：D25、D11

**交付**：生产 `PluginContext`、`runtime/access/` 资源服务及其测试。

**要点**（技术方案 §7.5）

- 资源服务（`fs` / `net` / `shell` / `secret`）统一 Workspace 路径、SSRF 防护、
  子进程超时与 Secret 包装，但不是插件安全沙箱。
- `PluginContext.spawn_task()` 是插件创建后台任务的唯一途径；Host API 不暴露裸
  `asyncio.create_task`。
- 在 D16 已有 `host.py` 上补齐生产级 `PluginContext`，不得另建第二套 Host API 或复制
  注册分派逻辑。
- 文档中必须写明：插件是受信任的同进程代码，启用即完全信任；不可信代码必须在进程外
  隔离（`13.7`）。

**验收**

- 四个资源服务分别验证 Workspace、SSRF、超时和 Secret 缺失语义。
- 插件只能读到自己的配置块（`CFG-002`），尝试读他人配置无对应 API。
- 内建能力使用同一 PluginContext 与注册路径，无特权路径（`BAS-005`）。
- 文档中的隔离能力声明经评审确认。

**规模**：约 450 行 + 400 行测试。**风险**：中。

### D27 两阶段加载与事务性注册

**依赖**：D26、D06

**交付**：`kernel/plugins/loader.py`、`kernel/plugins/host.py`（扩展外部 PluginContext）、
`tests/plugins/test_loader.py`

**要点**（技术方案 §7.3）

- 阶段 A（校验，不导入实现）7 个子步骤；阶段 B（加载）按拓扑序 5 个子步骤。
- 阶段 B 用 `RegistrationBatch`（D06 已实现），setup 抛异常即整体回滚。
- 外部插件必须复用 D16 的 Host `NucleaAPI` 注册实现；本模块只能补充受限
  `PluginContext`、加载计划与错误编排，不得复制注册分派逻辑。
- 阶段 A 失败记入报告，继续启动不依赖该插件的部分。
- 未启用任何外部插件时，外部发现、依赖解析和生命周期阶段为空；Runtime 仍通过
  D16 的统一 Host API 注册 `BUILTIN_MANIFESTS`，实例照常启动（`PLG-007`）。

**验收**

| 场景 | 期望 |
| --- | --- |
| 重复插件 id | 阶段 A 错误 |
| SDK 不兼容 | 拒绝加载，不带病加载（`SDK-005`） |
| 依赖缺失 / 依赖成环 | 阶段 A 错误，指出环路（`PLG-003`） |
| 插件配置不合 schema | 阶段 A 错误，带字段路径 |
| setup 中途抛异常 | 已注册能力全部回滚，registry 无残留（`EDG-103`） |
| 插件失败 | 实例继续启动，失败记入报告（`PLG-004`） |
| 最终基础能力缺失 | Runtime 统一拒绝启动 |
| 全部插件禁用 | 实例按内建基线正常启动（`PLG-007`、`EDG-101`） |
| 覆盖目标不存在 | 启动错误（复用 D06 语义） |

- 插件不 import `nucleamind.kernel.*`（架构测试，`PLG-002`）。

**规模**：约 550 行 + 650 行测试。**风险**：高。分支最多的模块，验收表必须逐条对齐。

### D28 插件生命周期

**依赖**：D27

**交付**：`kernel/plugins/lifecycle.py`、`tests/plugins/test_lifecycle.py`

**要点**（技术方案 §7.4）

- 状态机 `DISCOVERED → VALIDATED → LOADED → STARTED → STOPPING → STOPPED`，
  任意阶段可进 `FAILED` 并记录阶段与原因。
- 停止顺序 = 启动拓扑序的逆序（`PLG-005`）。
- 停止超时 5000 ms 后放弃等待、记录事件、继续停止其余插件（`EDG-104`）。
- 禁用后清理全部痕迹：注销能力、取消订阅、cancel 该插件 task group（`EDG-105`）。

**验收**

- 停止顺序断言（构造 A→B→C 依赖链，断言停止顺序 C→B→A）。
- 故意 hang 的插件在超时后不阻塞进程退出。
- 禁用后：能力查询不到、事件订阅失效、后台任务已取消（三项分别断言）。
- 状态机非法转换被拒绝。

**规模**：约 400 行 + 450 行测试。**风险**：中高，异步资源清理是 `13.3` 点名的难点。

### D29 nm plugins 命令与诊断输出

**依赖**：D28、D22

**交付**：`builtins/commands_core/` 扩展、`nm plugins` 子命令、`tests/plugins/test_cli_plugins.py`

**要点**（技术方案 §10.4、§10.5）

- `nm plugins list / enable / disable / uninstall / purge`。
- `uninstall` 默认保留插件状态目录；`purge` 需 `--confirm` 且先打印将删除的路径与体积
  （`EDG-505`）。
- `nm capabilities` 打印 shadowed 关系，覆盖不静默。

**验收**

- `enable` / `disable` 只改配置，不在当前进程生效（首版不热更新，需求 §4.2）。
- `uninstall` 后状态目录仍存在。
- `purge --confirm` 前打印路径与体积；无 `--confirm` 时拒绝执行。
- `nm capabilities` 输出中 shadowed 关系可读且包含 provider 标识（`NFR-502`）。

**规模**：约 350 行 + 350 行测试。**风险**：低。

### D30 示例插件与 Plugin Runtime 验收 ★

**依赖**：D29

**交付**

```text
examples/plugins/nucleamind-plugin-echo-tool/       # 新增一个 TOOL 能力
examples/plugins/nucleamind-plugin-session-memory/  # 覆盖内建 SESSION_STORE 为内存实现
  两者均为完整独立发行包（pyproject + src/ + tests/），通过 entry point 被发现
tests/e2e/test_plugin_runtime.py
docs/ 插件开发入门文档
```

**要点**（技术方案 §13 M4）

`session-memory` 专门用于验证 SINGLETON arity 的覆盖路径与 `on_disable` 语义，
风险低且覆盖面关键。

**验收** —— 逐条对应需求 §16.2 的八个里程碑条件：

| # | 验收项 |
| --- | --- |
| 1 | 不修改 engine / orchestrator 即可加载外部插件 |
| 2 | 插件注册的工具参与真实 turn |
| 3 | 覆盖内建 session store，`nm capabilities` 显示 shadowed 关系 |
| 4 | 禁用后能力消失；恢复内建与否由 `on_disable` 显式配置决定 |
| 5 | 配置错误 / SDK 不兼容 / 运行失败三类各有稳定错误码与诊断输出 |
| 6 | 示例插件不 import `nucleamind.kernel.*` |
| 7 | 内建与插件 session store 通过同一套 `SessionStoreContract` |
| 8 | 原能力关键行为有回归测试或明确迁移说明 |

另加：插件开发文档中的示例代码可被测试直接执行（防文档漂移）。

**规模**：约 400 行示例 + 450 行 e2e。**风险**：低，但这是对外宣称「插件体系可用」的节点。

## 12. 阶段 8：旧路径切换与清理

### D31 遗留 Agent 路径切换与删除

**依赖**：D30

**交付**：`legacy/agent/` 删除、`legacy/cli/` 删除、`nm legacy` 子命令删除、
`runtime/legacy_entry.py` 与 D01 中对应的 `R6` 白名单删除、
`legacy/api/server.py` 与 WebUI gateway 改为调用新 Kernel、
`tests/baseline/` 与对应 `tests/legacy/` 用例删除

**要点**（技术方案 §13 M5 的五步法）

到此为止新 Kernel 已具备完整内建能力与插件体系，`legacy/agent/` 的存在意义结束。
本模块的目标是**删除它**，而不是给它搭桥。

`legacy/agent/AgentLoop` 的 4 个调用方分别处置：

| 调用方 | 处置 |
| --- | --- |
| `legacy/cli/agent.py`、`legacy/cli/commands.py` | 删除。`nm` 已有完整 CLI（`D23`） |
| `legacy/api/server.py` | 改为调用 `runtime/instance.py`；本身在 `D32+` 迁为插件 |
| `legacy/nanobot.py` SDK 门面 | 删除，由 `embed/` 取代（`D23` 已落地） |

**不设 `runtime.engine = legacy | kernel` 双路径开关。** 早先版本计划过这个开关，
但它要求两条路径长期共存、双份测试、且必须解决「谁能写 session」的冲突，
成本远高于收益。本项目用 git 回退即可，不需要运行期开关。

同一 PR 内必须完成删除，不留「以后再清」的副本——这是技术方案 §13 M5 第 d、e 步的要求。

**实际落地与本节的三处偏差（`D31` 完成后回写）**

1. **WebUI 与 gateway 一并删除，没有改调新 Kernel。** 上表只列了 `AgentLoop` 的四个调用方，
   但 `legacy/webui/` 的 32 个模块要的是 agent 的 `ToolRegistry`、`SkillsLoader`、
   MCP 生命周期与 token-usage Hook——新 Kernel 一样都没有，那是 `D32+` 的工作量而不是
   200 行改造。删掉 `legacy/cli/` 之后 gateway / webui / api 本来也失去了唯一启动入口。
   `webui/` 前端源码保留但暂时没有后端。
2. **OpenAI 兼容接口是新层重写的官方插件，不是改造 `legacy/api/server.py`。**
   落点 `plugins/nucleamind-plugin-openai-api/`（一条 `CHANNEL` 能力）+ 通用无头命令
   `nm serve`。做成 Channel 是硬约束：出站增量只经 `deliver` 路由回注册过的 Channel，
   `instance.submit()` 要等整条 turn 跑完才返回，用它做不出 SSE。
3. **删除范围是「Agent 路径及其依赖方」，不是整个 `legacy/`。** 保留 `providers/`、
   `channels/`（去掉 websocket）、`session/`、`cron/`、`command/`、`config/`、`utils/`、
   `bus/`、`security/` 等不依赖 agent 的库代码，作为 `D32+` 的在树迁移源；它们此后不可达。
   债务从 352 文件 / 133317 行降到 225 文件 / 77040 行。

**验收**

- `legacy/agent/`、`legacy/cli/` 目录不再存在，全仓库无悬挂引用
  （`rg "legacy\.agent|legacy\.cli" src/ tests/` 无命中）。
- `nm legacy` 子命令已删除，`nm --help` 中不再出现。
- `runtime/legacy_entry.py` 与架构测试中的精确白名单已删除；此后 `R6` 恢复为无例外。
- `legacy/api/server.py` 的现有测试在改用新 Kernel 后全部通过。
- `tests/baseline/` 与 `legacy/agent` 相关的 `tests/legacy/` 用例已删除。
- `scripts/legacy_debt.py` 数字显著下降（约 19000 行 agent + 6500 行 cli）。
- 端到端：`nm` 交互式会话与 `legacy/api/server.py` 的 OpenAI 兼容接口均可完成一次 turn。

**规模**：主要是删除 + `api/server.py` 约 200 行改造。
**风险**：中。风险从「双路径长期共存」降为「一次性切换」，但需确认 `api/server.py`
与 WebUI gateway 的行为不回退——这两处的现有测试是主要保障。

后续 `D32+`（阶段三 P1 的能力插件化：Model / Memory / Tool / Channel / Cron / WebUI）
按技术方案 §13 M5 的顺序与五步法逐个立项，每个模块独立编号、独立验收。
每迁完一个模块，同 PR 内删除 `legacy/` 对应目录与其 `tests/baseline/`、`tests/legacy/`
用例——`legacy/` 债务指标不下降即视为该模块未完成。
`legacy/` 清空后删除该目录、`R6` 守卫与 `scripts/legacy_debt.py`。
本文档在 D31 完成后再补充这部分拆分。

## 13. 汇总表

| # | 模块 | 阶段 | 依赖 | 规模（行） | 风险 |
| --- | --- | --- | --- | --- | --- |
| D00 | 仓库重构与包重命名 | 0 | — | 250 + 配置 | 中 |
| D01 | 架构守卫与 CI 门禁 | 0 | D00 | 500 | 低 |
| D02 | 契约·基础层 | 1 | D01 | 350 | 低 |
| D03 | 契约·领域与执行层 | 1 | D02 | 600 | 中 |
| D04 | 契约·能力层 | 1 | D03 | 450 | 中 |
| D05 | sdk 骨架与夹具 | 1 | D04 | 500 | 中 |
| D06 | Capability Registry | 1 | D05 | 950 | 中高 |
| D07 | 旧实现行为基线 | 2 | D01 | 500 | 低 |
| D08 | 取消与预算 | 2 | D04 | 650 | 低 |
| D09 | Turn Engine | 2 | D06 D08 | 1000 | 高 |
| D10 | 实例布局与配置 | 3 | D02 | 850 | 中 |
| D11 | Secret 与凭据 | 3 | D10 | 450 | 低 |
| D12 | 可观测性 | 3 | D02 | 750 | 低 |
| D13 | 分流与 Session 并发 | 4 | D06 D10 | 850 | 中 |
| D14 | Turn Orchestrator | 4 | D09 D12 D13 | 1200 | 高 |
| D15 | 骨架集成验收 ★ | 4 | D14 | 300 | 低 |
| D16 | 内建加载与契约套件 | 5 | D15 | 950 | 中 |
| D17 | session_jsonl | 5 | D16 | 650 | 中 |
| D18 | context_basic | 5 | D16 | 500 | 低 |
| D19 | model_openai | 5 | D16 | 950 | 中 |
| D20 | tools_fs | 5 | D16 | 1150 | 中高 |
| D21 | tools_shell | 5 | D16 | 850 | 高 |
| D22 | commands_core | 5 | D16 D13 | 750 | 低 |
| D23 | cli_entry + runtime/embed + nm 入口 | 5 | D17–D22 D14 | 1300 | 中 |
| D24 | 开箱可用验收 ★ | 6 | D17–D23 | 750 | 中 |
| D25 | Manifest 与发现 | 7 | D24 | 900 | 中 |
| D26 | 资源门面与 Context | 7 | D25 D11 | 850 | 中 |
| D27 | 两阶段加载 | 7 | D26 D06 | 1200 | 高 |
| D28 | 插件生命周期 | 7 | D27 | 850 | 中高 |
| D29 | nm plugins 与诊断 | 7 | D28 D22 | 700 | 低 |
| D30 | 示例插件与验收 ★ | 7 | D29 | 850 | 低 |
| D31 | 遗留 Agent 路径切换与删除 | 8 | D30 | 200 + 删除 | 中 |
| D32 | 官方插件：Anthropic Model Provider | 三 P1 | D30 | 1400 | 中 |
| D33 | Channel 泵扇出 + 官方 Discord 插件 | 三 P1 | D30 | 1500 | 中 |
| D34 | 官方 Feishu Channel 插件 | 三 P1 | D33 | 5000 | 中 |
| D35 | 删除 legacy/ 与 webui/，迁移期设施退休 | 三 P1 | D34 | 删除 21.5 万行 | 中 |

规模为实现 + 测试的粗估，用于判断模块是否需要再拆，不作为工期承诺。

> **`D32`–`D35` 是阶段三 P1（M5 能力插件化）的模块，不属于原本的 `D00`–`D31` 规划。**
> `D35` 之后项目范围收窄：Model Provider 止步、Channel 只做 feishu、WebUI 不做，
> M5 只剩 Memory / 扩展 Tool / Cron 三项。下面 `D00`–`D31` 的逐模块拆分是**历史记录**，
> 其中提到 `legacy/`、`R6` 与债务棘轮的部分描述的是当时的状态，`D35` 已全部删除。

高风险模块共 4 个：`D09`、`D14`、`D21`、`D27`。这几个建议单独评审实现方案后再动手。

`D00` 虽列为中风险，但它是全部后续模块的地基：做不干净（漏改路径、混入行为变更）
会影响后面每一个模块。建议单独一个 PR、单独评审。

## 14. 进度跟踪

在 [`README.md`](./README.md) 中维护一行式进度，不在本文档内记录状态：

```text
当前进度：D00 ✅  D01 ✅  D02 ✅  D03 ✅  D04– ⬜
```

模块完成时更新该行；阶段完成时在 `README.md` 的「已完成」中补一段阶段小结，
说明实际交付与技术方案的偏差。本文档只在模块拆分本身需要调整时修订。

## 15. 与技术方案的对应关系

| 技术方案节 | 对应模块 |
| --- | --- |
| §4.1–§4.5 仓库与包结构、§13 M-A | D00 |
| §3.1 依赖规则、§12.1 可读性、§12.3–§12.4 测试与 CI | D01 |
| §5 公开数据契约 | D02 D03 D04 |
| §6.1 Registry 与覆盖 | D06 |
| §6.2 Turn 两层拆分 | D09 D14 |
| §6.3 输入分流 | D13 |
| §6.4 取消与预算 | D08 |
| §6.5 Session 并发与写入 | D13 D17 |
| §6.6 Hook 与事件 | D12 D14 |
| §6.7 配置与 Secret | D10 D11 |
| §6.8 可观测性 | D12 D22 |
| §7.1–§7.2 发现与 Manifest | D25 |
| §7.3 两阶段加载 | D27 |
| §7.4 生命周期 | D28 |
| §7.5 Host API 与信任边界 | D26 |
| §7.6 SDK 版本策略 | D05 |
| §8 内建默认能力 | D16–D23 |
| §9 Message 与 Channel | D03 D23 |
| §10.1 启动流程 | D10 D24 D27 |
| §10.2 turn 流程 | D14 D15 |
| §10.3 中断流程 | D08 D14 D23 |
| §10.4–§10.5 插件变更 | D29 |
| §11 目录布局与多实例 | D10 |
| §12.3 测试分层 | D01 D16 |
| §13 M1–M4 | D01–D30 |
| §13 M5–M6 | D32+（待立项） |
