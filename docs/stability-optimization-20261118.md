# 稳定性与空闲占用优化

任务 TASK-5503.5，承接 2026-11-17 交叉验证，开发基线为 `1c5318f`。

## 前端开工

### 代表任务

用户选择托管模型并读取积分说明，或切换 BYOK 服务商及接口后重新填写密钥。保存期间继续编辑时，应保留更新的输入与既有导航状态。

### 本次变化

修正模型目录倍率和选中模型的费用提示，同步服务端清空 Key 的回执。后台处理恢复退出时的重试额度，重复账户校验复用成功的签名检查。

### 已有条件

沿用 [settings.js](C:/Ansel_Work/B-软件开发/FoilPage-视频的二向箔/FoilPage-source/app/webview_ui/js/settings.js) 的保存队列、字段 revision、可访问控件和草稿恢复。账户校验依据 [license.py](C:/Ansel_Work/B-软件开发/FoilPage-视频的二向箔/FoilPage-source/engine/license.py) 与 [account_access.py](C:/Ansel_Work/B-软件开发/FoilPage-视频的二向箔/FoilPage-source/engine/account_access.py)，任务恢复依据 [queue_store.py](C:/Ansel_Work/B-软件开发/FoilPage-视频的二向箔/FoilPage-source/engine/queue_store.py) 和 [worker.py](C:/Ansel_Work/B-软件开发/FoilPage-视频的二向箔/FoilPage-source/app/worker.py)。界面宿主为 WebView2，既有浏览器测试使用 Playwright 与合成资料。

### 验证办法

覆盖倍率 0.5、1、3、5 和切换后的提示，检查旧 Key 清空、排队的新 Key 与外部设置更新。任务覆盖多次退出、用户取消、租约归属、旧版中断记录及新运行的 AI 操作身份。验签缓存覆盖过期、设备、密钥、载荷篡改与可变返回值，使用同一合成证书做前后耗时对照。

## 实现

- 积分目录保留 `cost_multiplier`，模型保存完成后更新费用说明。
- 保存服务商或接口时，Key 的连带变更纳入字段 revision。服务端清空标志随当次回执传递，界面显示重新填写提示。
- 优雅退出归还本次领取消耗的尝试次数，并验证领取者。旧版退出记录在再次领取时归还最近一次中断的额度。AI 每次运行使用独立操作 ID，支持恢复后的托管阶段重新执行。
- Python 标准库 LRU 缓存最多复用 64 个成功的 Ed25519 签名检查。过期、设备、账户和会话判断逐次执行，载荷逐次解码。
- VM 验收脚本使用当前 Worker 文件名。README 与 Node 元数据纳入统一版本检查。

## 验证记录

隔离变更已通过 2008 项 Python、654 项 JavaScript、424 项 Agent 专项和四组 UI 检查。补充的密钥草稿与窄窗口检查随设置专项共 14 项通过。Python 套件的 15 项跳过继续按平台、MCP 环境和显式实跑开关记录。

同一合成证书执行 40 次账户检查时，签名验证由 40 次降为 1 次。包含 cProfile 与分配跟踪的对照中，进程 CPU 时间由 18.766 秒降为 0.438 秒，减少约 97.7%。该微基准覆盖重复账户检查，应用整体占用需要实装测量。

原始证据位于 `.ui-audit/optimization-20261117/`，隔离工作区位于该目录的 `worktree/`。TASK-5520 的素材关联与文稿对照改动已进入主工作区提交，合并验证将记录其具体内容指纹。
