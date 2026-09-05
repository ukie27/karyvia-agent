# Security Policy

## 项目定位

Karyvia 基于 HKUDS/nanobot（MIT 协议）独立开发。本项目目前处于重构阶段，
不作为公共基础设施运营；安全修复按本仓库的常规 git 提交进行。

## 安全最佳实践（开发与部署均适用）

### 1. API Key 管理

**关键**：永远不要把 API key 提交进版本控制。

```bash
# ✅ 最佳：配置中使用环境变量引用（密钥不落盘）
# ~/.nanobot/config.json:
#   "apiKey": "${ANTHROPIC_API_KEY}"
# 运行时通过环境变量或 Docker secret 提供。

# ✅ 良好：配置文件限制权限
chmod 600 ~/.nanobot/config.json

# ❌ 禁止：在代码或配置文件里硬编码明文 key
```

注意：`config/loader.py` 中的 `${VAR}` 不是 shell 默认值语法——环境变量缺失时加载会抛 `ValueError` 并回退默认配置。

### 2. 凭据管理建议

- 优先环境变量引用；明文 key 存入 `~/.nanobot/config.json` 时设置 `0600` 权限。
- 生产部署考虑 OS keyring / 凭据管理器。
- 定期轮换 API key；开发与生产环境使用不同 key。

### 3. 代码中的安全边界（不可绕过）

- Agent 工具的网络请求必须走 `security/network.py` 的 URL 守卫（防 SSRF），不要在工具里直接 `httpx.get` / `requests.get`。
- 文件系统工具必须走 workspace path resolver（防路径穿越），新路径逻辑必须做等价包含检查。
- Shell 执行受工作区守卫与可选沙箱（目前仅 bwrap 后端）约束。
- 详细边界见 [.agent/security.md](.agent/security.md)。

## 报告漏洞

如发现安全漏洞，请通过本仓库的 issue tracker 联系维护者。报告中不要包含可用的
API key、token、cookie、私有地址或其他敏感信息。
