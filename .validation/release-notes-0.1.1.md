# FoilPage 0.1.1

0.1.0 首个源码基线上的依赖安全补丁，修复 Dependabot 报告的六项依赖风险。功能行为与 0.1.0 一致，无新增特性。

## 依赖升级

| 组件 | 修复版本 |
| --- | --- |
| Pillow | 13.0.0 |
| Brotli | 1.2.1 |
| filelock | 4.2.1 |
| setuptools | 84.1.0（base 锁） |
| fonttools | 4.60.2 |
| pytest | 9.0.3（桌面及 Agent 构建工具） |

版本及 Python 3.11 / Windows 轮子经官方 PyPI 元数据核对。运行哈希锁仅上述四个运行包变化，其余运行组件保持原版；构建工具链包不影响运行锁。

## 验证

本机 CPU 隔离环境 1973 项通过、15 项按环境跳过；独立 Agent 环境 440 项通过；Pillow 实际图标解码与合成 PNG 往返、fonttools / Brotli 对 47,871 字形 WOFF2 完整往返通过。远端 Windows Python、前端、Agent 及打包三项门禁在合并提交 75f7bfc 上全部成功（run 43447180530）。合并后 Dependabot 告警仅余 1 条 open（medium，opentelemetry-api，升级 PR 已开且门禁已绿）。

## 获取与使用

下方 Source code ZIP / tar.gz 对应本标签源码。开发、启动和打包步骤见 README，许可与第三方归属见 LICENSE 和 THIRD_PARTY_NOTICES.md。本次为源码补丁：不替换历史 1.x 安装包，也不包含 Windows 安装器或 GPU 包；官方托管 AI 由独立服务提供，按账户订阅和额度使用。
