# FoilPage 0.1.3

单仓一体化发行首版：从本版起，**源码归档与 Windows 安装器、GPU 包在同一标签下发布**，版本号源码与安装包一致。内容对齐私有工程主线（含 11-15 防重复提交修复轮与视频扫描取消等全部改进）。

## 获取

- `FoilPage-Setup-0.1.3.exe` — Windows 10/11 安装器（核心程序；NVIDIA GPU 加速可选装 GPU 包）
- `FoilPage-GPU-Pack-0.1.3.zip` — GPU 加速包（解压到 Worker 目录即可，准备页可自选安装）
- `RELEASE-MANIFEST-0.1.3.json` / `SHA256SUMS.txt` — 构建来源与校验

下方 Source code ZIP / tar.gz 为同标签源码。

## 版本线说明

- 0.1.x 为当前唯一活跃版本线，源码与安装包同号同发。
- 历史 1.x 安装版（最高 1.3.12）位于 [FoilPage-releases](https://github.com/Ansel-Poiesis/FoilPage-releases)（已停发存档）。**1.x 安装不会自动升级到 0.1.x**：版本线更替后更新检查不再提示，请按应用内迁移公告手动下载安装；本地资料目录兼容，无需迁移数据。
- 原私有开发仓库已停更冻结，客户端开发只在本仓库进行。

## 验证

- 构建源提交 `69ff37f`（干净工作树，构建时 tree 锁定见 RELEASE-MANIFEST）。
- 本机全量验证门禁通过：Python 1979 项通过、按环境跳过若干；Node/界面冒烟与发布清单核对通过；远端三门禁（windows-python / frontend / agent）在该提交成功。

## 许可

自有源码 MPL-2.0；第三方归属见 LICENSE 与 THIRD_PARTY_NOTICES.md。官方托管 AI 由独立服务提供，按账户订阅和额度使用。
