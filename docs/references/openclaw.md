# OpenClaw 参考导航

## 定位

`references/openclaw` 是 Karyvia 研究插件生态、插件宿主边界和 OpenClaw 兼容层的主要参考项目。OpenClaw 规模很大，默认只研究公开契约和最小官方插件实例。

## 优先入口

- 插件 SDK 规则：`references/openclaw/src/plugin-sdk/AGENTS.md`
- SDK 包定义：`references/openclaw/packages/plugin-sdk/package.json`
- 插件 SDK 源码：`references/openclaw/src/plugin-sdk/`
- 插件加载与注册：`references/openclaw/src/plugins/`
- 插件文档：`references/openclaw/docs/plugins/`
- 插件包契约：`references/openclaw/packages/plugin-package-contract/`
- Provider 接口：`references/openclaw/src/plugin-sdk/provider-entry.ts`
- Provider 认证：`references/openclaw/src/plugin-sdk/provider-auth.ts`
- Channel 入口：`references/openclaw/src/channels/`
- Gateway 协议：`references/openclaw/packages/gateway-protocol/`
- Memory SDK：`references/openclaw/packages/memory-host-sdk/`

## 推荐读取顺序

```text
manifest / package contract
    -> public SDK entrypoint
    -> plugin loader
    -> lifecycle and registration
    -> one minimal official plugin
    -> tests and migration docs
```

## 适合查询的问题

- 插件如何被发现、校验、加载和卸载？
- 插件与宿主之间有哪些稳定的 SDK 边界？
- Provider、Channel、Memory 等扩展如何注册？
- 哪些能力通过窄 SDK 子路径暴露，哪些实现被宿主隐藏？
- OpenClaw 插件兼容层需要实现哪些协议转换和能力降级？

## 读取限制

不要先阅读全部 `extensions/`。除非正在验证一个已确认的公共契约，否则只选择一个小型官方插件作为实现样本，并优先读取它的 manifest、入口、SDK import 和测试。
