# Security Boundaries

Agent 拥有相当大的权力（文件系统、shell、出网）。修改相关代码时，以下守卫不可绕过。

**贯穿全文的一条**：这些都是**应用级**守卫，不是进程隔离。同进程的插件可以绕过全部门面
直接 `import os`。这句话写在 `sdk/api.py`、`runtime/access/__init__.py` 与
`docs/permissions.md` 里，是必须保留的诚实声明而不是免责套话。

## 工作区路径限制

文件工具（`fs.read` / `fs.write` / `fs.edit` / `fs.list` / `fs.grep`）的路径守卫在
`builtins/tools_fs/paths.py`。**两道校验缺一不可**：

1. **逻辑校验**（`normpath`）挡 `..`；
2. **realpath 校验**（`resolve`）挡符号链接与重解析点。

两次比较都过 `os.path.normcase`。不做 `expanduser`，绝对路径接受但过同一道门，
Windows 保留设备名两个平台一律拒绝。越界错误的 `detail` **只放原始串**——宿主机绝对
路径进到模型可见的错误里就是泄漏。

TOCTOU 挡不住，docstring 里如实写着，**别删掉那段**。

同一套判定还有两份独立实现：`builtins/tools_shell/paths.py::CwdGuard`（守 shell 的 cwd）
与 `runtime/access/` 的 `ctx.fs` 门面。三份刻意不共享（`R4` 够不着，且它们是可各自被禁用
的独立提供方），由逐条对照测试钉住——**改一边要改多边**。

**守住 cwd 不等于守住命令能碰到的文件**（`cat /etc/shadow` 与 cwd 无关）。

**规则**：任何新的路径处理逻辑必须走上述守卫，或做等价的包含性检查并区分读/写能力。

## SSRF 防护

`ctx.net`（`runtime/access/`）提供统一的受守卫 HTTP 服务。它判的是
**DNS 解析之后**的地址，并**手动跟随重定向**、对每一跳重新校验：私有网段、回环、
链路本地与云元数据地址（含 `169.254.169.254`）一律拒绝。

**规则**：希望获得统一 SSRF 防护时使用 `ctx.net`。插件仍可直接使用网络库，因为安装并启用
插件即完全信任其代码；不应把这个服务描述成出网权限或安全沙箱。

## Shell 执行

`shell.exec`（`builtins/tools_shell/`）的三条边界：

- **子进程环境是白名单**（`environ.py`）：父进程环境默认一个字节都不进子进程，只有平台
  基线与运维在 `pass_env` 里点名的才转发。**不要改成黑名单**——那要求穷举所有会泄漏的
  变量名，漏一个就把凭据交给模型写的命令。哨兵用例走真实子进程打印自己的环境。
- **取消是三步**（`process.py::_supervise`）：终止信号 → 等宽限期 → 强杀 + 收尸。
  直接 `kill` 会让一条 `rm -rf` 写了一半就停，那不叫取消成功。
- **副作用三档判定只在 `executor.py::_fold` 一处**：执行**之前**失败 → `NONE`；
  进程自己退出或宽限期内被终止 → `OCCURRED`；**宽限期用尽被强杀 → `UNKNOWN`**
  （全项目唯一的 `SideEffect.UNKNOWN` 产出点）。

没有进程级沙箱。容器化部署时用容器本身做隔离。

## 凭据

**`contracts.SecretStr` 是全项目唯一的密钥包装类型**。它刻意不是 dataclass
（`dataclasses.asdict` 会抖出明文），`str` / `repr` / `format` 恒为 `MASK`，
明文只经 `reveal` 取出。

配置树自始至终只持有 `${VAR}` 字面量，解析后的明文不进配置文档
（`kernel/config/secrets.py`）——写回因此「没有别的东西可写」。

脱敏在**构造时**完成（`contracts.errors.redact` / `scrub`），不依赖日志或 sink 层。
事件载荷在 `RuntimeEvent` 构造**之前**已经过 `prepare_payload`。
**不要在 sink 或调用点补第二道脱敏**，也不要新写敏感键名规则。
