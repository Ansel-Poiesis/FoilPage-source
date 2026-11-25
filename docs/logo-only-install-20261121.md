# 仅替换回卷耳标志的本机实装（2026-11-21）

任务：TASK-5542。先生明确撤回绿色主题优化，要求保留原界面，只采用选定的回卷耳 logo 并完成本机实装。

## 范围与恢复

本轮对 `app.css`、`product.css`、`setup.css` 和 `settings.js` 的修改均已撤回，四份文件与 `.ui-audit/brand-green-20261121/source-before/` 中的开工快照逐字节一致。恢复前已核对上一轮产物指纹并备份，保留了开工前已有的其他修改。

恢复记录：`.ui-audit/logo-only-install-20261121/source-restore.json`。恢复后的 `node tests/ui_product_smoke.cjs` 通过，覆盖 1280/960/640/560px。

## 选定图标

图形为先生附件所选的回卷耳版本，母版 SHA256 为 `89f68c5cbb676a93985b48c034f6645571192d1026d5f44b3d41173604b9d7de`。相关资源与生成方式见 [图标说明](assets/icons/README.md)。

## 安装准备

现役入口为 `C:/Users/mingc/AppData/Local/Programs/FoilPage/FoilPage-Desktop.exe`。实装清单仅包括主界面 2 份 PNG、系统 PNG/ICO 和扩展 4 个尺寸图标，共 8 个资源文件；不更换 EXE、CSS、设置脚本或用户资料。

安装前备份与源/目标指纹位于 `.ui-audit/logo-only-install-20261121/install.json` 和同目录 `backup/`。开始菜单快捷方式已引用外置 ICO，无需修改目标程序或重打包现有业务代码。

## 实装结果

已在后台汇总进入空闲时通过现有本地接口正常退出，原子替换 8 份图标资源，并从原安装入口启动。安装资源 SHA256 均与已验证源码一致；EXE、所有安装版 CSS 与设置脚本的指纹前后相同，资料数据库的任务、下载、媒体与文稿清单数量前后一致，Worker 正常恢复。

已通过 computer-use 查看安装版实际主窗口：侧栏显示选定回卷耳标志，原有深色材质、布局、导航与任务列表保留。实际 WebView 服务返回的标志 PNG 指纹也与新资源一致。现场记录位于 `.ui-audit/logo-only-install-20261121/live-ui.json`。

仅更新了图标资源与源码的对应生成源，没有将已否定的配色方案装入客户端，也没有更换程序二进制或发布新版本。扩展的四个尺寸图标文件已同步；本轮未重载用户正在使用的浏览器扩展。资源管理器直接查看 EXE 文件时的内嵌图标属于原二进制资源，本轮保持不动；开始菜单、运行窗口与托盘沿现有外置 ICO 加载。
