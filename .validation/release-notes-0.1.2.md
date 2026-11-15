# FoilPage 0.1.2

防重复提交与失败指引补丁，外加一项依赖升级；功能行为对齐私有 1.x 工程同轮修复。

## 修复

- B 站 / YouTube / 弹窗入口接入与抖音页相同的提交预检：任务进行中阻止、已下载完成需确认；**10 分钟内失败过的视频在任意入口重提均需确认**，避免反复提交加剧平台风控。
- 同 URL 的未终态队列身份不再按"下载 / 转写"意图分叉：未派发的凭据直接并入转写旗标，已派发的以新凭据交服务端按活跃任务合并。
- 通知循环对数据库瞬时错误（如误报 disk full）指数退避，首错附磁盘剩余 / 库体积诊断，不再逐秒刷屏。
- 本地解析与浏览器通道均被平台拒绝时，终态错误整合两通道事实与可操作指引（登录态、网页验证、VPN、勿短窗反复重试）。

## 依赖

- opentelemetry-api 1.45.1 → 1.45.2（`requirements-agent.lock.txt`），关闭最后一条 Dependabot 告警（medium）。

## 验证

- 扩展四套 184/184；pytest 下载管线 + 通知退避 79 通过 / 1 项按环境跳过；`sync-extension-main --check`、`check_source.py`（378 个跟踪文件）通过。
- 三项远端门禁（windows-python / frontend / agent）在本次修复 PR 与依赖升级 PR 上均成功；标签提交的 main push 门禁以该提交 Actions 记录为准。

## 获取与使用

下方 Source code ZIP / tar.gz 对应本标签源码。开发、启动和打包步骤见 README，许可与第三方归属见 LICENSE 和 THIRD_PARTY_NOTICES.md。本次为源码补丁：不替换历史 1.x 安装包，也不包含 Windows 安装器或 GPU 包；官方托管 AI 由独立服务提供，按账户订阅和额度使用。
