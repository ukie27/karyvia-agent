# Common Gotchas

## 不要运行 `ruff format`

历史代码未经 `ruff format` 排版，整体运行会生成大面积无关 diff。只用 `ruff check`。

## 沙箱下跑 pytest 要加 `--basetemp=.pytest-tmp`

系统临时目录不可写时 `tmp_path` 夹具会以 `PermissionError` 报错，而那与被测代码无关。
`.pytest-tmp/` 已在 `.gitignore` 里。

## 插件的测试模块名要带插件前缀

`testpaths` 一次收集整个 `plugins/`，而 pytest 按**模块名**去重。两个插件各有一个
`test_stream.py` 或 `_fakes.py` 时，先被导入的会顶掉后一个，另一棵测试树整体
`ImportError`。**单独跑各自的目录看不出来，跑全量才炸。**
照 `plugins/karyvia-plugin-feishu/tests/` 的命名切。

## 配置的 `${VAR}` 引用

`kernel/config/secrets.py` 在加载时解析 `config.json` 里的 `${VAR}`。这**不是**
shell 那种带默认值的语法：

- 没有 `${VAR:-默认值}` 回退，没有 `$${VAR}` 转义；
- 空变量按**缺失**处理；
- 任何位置的引用都算密钥（整串或内嵌）；
- 缺失即 `CONFIG_SECRET_MISSING`，不静默回落到默认配置。

解析结果是按 JSON Pointer 索引的 `SecretMap`，**配置树自始至终持有 `${VAR}` 字面量**。
要落盘一份配置前先过 `prepare_for_write()`。

## Windows 兼容

Karyvia 明确支持 Windows。几处容易踩的：

- **起子进程必须走 `create_subprocess_shell`**（`builtins/tools_shell/command.py` 的模块
  docstring 是唯一出处）：`cmd.exe` 接在 `/c` 后的是原始命令行尾巴，而 `subprocess` 用
  `list2cmdline()` 把 argv 拼回字符串时会把内层引号转义成 `\"`，`cmd` 不认识——任何带
  引号的命令当场残掉。平台分派只在 `process._spawn` 一处，有测试数 `os.name` 的出现次数。
- **判断 PID 是否存活一律用 `kernel/config/process.py::process_is_alive()`**，绝不用
  `os.kill(pid, 0)`：Windows 上 CPython 把非 CTRL 信号映射到 `TerminateProcess`，
  那个「探测」会**杀掉目标进程**。返回值是三态，`UNKNOWN` 不得用来回收锁。
- 一律用 `pathlib.Path`，不要假设 `/` 分隔符；路径比较过 `os.path.normcase`。
- 测试里拦网络要拦 `connect` / `connect_ex` / `getaddrinfo` 的**目标**并放行回环，
  **不要**拦 `socket.socket` 的构造——`ProactorEventLoop` 用 `socketpair()` 做 self-pipe，
  那样只会证明事件循环起不来。

## 上下文污染会一直留着

写进会话历史、Context 片段或事件载荷的任何东西，都可能在未来的 LLM 调用里被重放。
时间戳、本地媒体路径、工具调用回声、原始 fallback 转储在成为模型模仿的样例之前，
必须有界且已脱敏。

**`trust=SYSTEM` 是进入系统指令位置的唯一凭据**，`kind` 不参与判定；`UNTRUSTED` 的包裹
由契约层的 `as_model_text()` 完成，组装器不许自己拼字符串。运维配置的自定义指令是
`TrustLevel.OPERATOR` 而不是 `SYSTEM`——它因此进不了 system 消息位置（`CMD-005`）。

## 会话写入的原子性

`builtins/session_jsonl/` 的 `committed_bytes` 提交水位是整批原子性的**全部**机制：
读只认水位内的字节，写最后才换 `meta.json`。文件比水位**短**是损坏，不是「就这些了」。
不要把它换成普通的 `open(..., "w")`。

`builtins/tools_fs/` 的写走「临时文件 → `fsync` → `os.replace`」，替换成功后没有可失败
的步骤，因此它一次 `SideEffect.UNKNOWN` 都不产出。

## 事件订阅者是同步的

签名是 `Callable[[RuntimeEvent], None]`，Kernel 在**发布事件的同一个栈**里调它——任何
`await` 都会把 turn 的执行卡在回调上。要异步处理就在回调里 `put_nowait` 进自己的**有界**
队列，再由一条后台任务消费（`plugins/karyvia-plugin-feishu/` 的 `_drain_hints`）。

连续 5 次失败、或单次投递超过 50 ms 达 5 次，订阅者会被**自动退订**——查 `bus.health()`。
