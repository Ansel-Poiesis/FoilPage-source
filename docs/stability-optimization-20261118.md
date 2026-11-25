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
- 素材列表建立历史任务关联时，复用已登记的规范路径，并缓存字符串形式的路径键。离线历史目录参与列表查询时可避免额外的文件系统解析。

## 组合验证

本轮改动与 TASK-5520 的素材关联、文稿对照交互组合验证。组合测试首先复现了历史任务夹具缺少 ID 的失败，补齐夹具后又验证了离线路径产生文件系统解析的问题。修复历史路径索引后，相关 107 项回归通过，完整 Python 套件复跑通过。

| 检查 | 结果 |
| --- | --- |
| 完整 Python | 2010 通过，15 跳过 |
| JavaScript | 672 通过，0 失败 |
| Agent 专项环境 | 424 通过，与完整 Python 含部分交叉覆盖 |
| 既有 UI 脚本 | 四组通过 |
| UI 状态矩阵 | 313 个状态通过，覆盖三个窗口及字号组合 |
| 本机安装版本资源 | 4 项通过，Desktop 与 Worker 均为 0.1.5.0 |
| 打包启动冒烟 | Desktop 独立数据目录冒烟退出码 0 |

设置专项覆盖倍率、Key 清空回执、排队的新 Key、未保存草稿、外部同步及 560×500 窗口下 130% 字号。任务专项覆盖取消、租约、最后一次尝试、重复退出及旧版中断记录。验签专项覆盖过期、设备、换钥、载荷篡改、缓存上限及独立可变返回值。

完整 Python 的跳过项包括 MCP 环境 4 项、Windows 符号链接权限 5 项、GPU 实跑开关 1 项、安装包路径开关 2 项、真实平台网络开关 3 项。其中 MCP 已用 Agent 专项环境复验，安装版本资源已通过显式安装路径复验。

## 占用测量

同一合成证书执行 40 次账户检查时，签名验证由 40 次降为 1 次。包含 cProfile 与分配跟踪的对照中，进程 CPU 时间由 18.766 秒降为 0.438 秒，减少约 97.7%。缓存容量固定为 64 个成功签名字节组合。

本机实装前后均在队列空闲、502 条历史任务的条件下采样，每轮约 20 秒。表中 CPU 百分比采用单核口径。更新后启动预热，再采集两轮，数值按先后列出。

| 进程 | 更新前 CPU | 更新后 CPU | 更新前工作集 / 私有内存 | 更新后工作集 / 私有内存 |
| --- | --- | --- | --- | --- |
| Desktop | 35.37% | 7.81%、6.71% | 156.49 / 109.68 MiB | 148.43 / 106.36、143.46 / 101.19 MiB |
| Worker | 10.67% | 0.23%、0.08% | 36.34 / 23.34 MiB | 35.14 / 23.40、35.16 / 23.39 MiB |

两轮更新后 CPU 按采样时长加权后，Desktop 下降约 79.5%，Worker 下降约 98.5%。内存为对应时间点快照，启动时长及系统分页会影响工作集。Worker 私有内存增加约 0.05 MiB。

## 本机同步

应用继续使用 0.1.5 版本标记。本轮源码补丁已接入主工作区，代码提交为 `0361b5f` 与 `83cef44`。组合验证与打包工作树提交为 `ede882d3d473e9ab850d0973b08d42132e849bf5`，其代码内容与主工作区一致，主工作区另含 TASK-5520 的安装记录。

本机构建完成后，为 52 个变更文件建立 SHA256 清单及原文件备份，变更文件共 36,629,802 字节。正常退出时确认队列处于空闲状态。首次原位写入 Desktop 可执行文件遭拒，50 个资源文件先行写入。随后再次按可执行文件路径核实进程退出，使用同目录暂存与原子替换完成两个可执行文件更新。最终重新核验全部 52 个变更文件，并将新 Desktop 与 Worker 分发目录的 1641 个文件逐一比对安装目录，全部一致。

安装目录为 `C:/Users/mingc/AppData/Local/Programs/FoilPage`。重启后通过已安装 Agent 读取应用入口，队列仍为 502 条、完成 422 条、活动 0 条，Worker 运行正常。素材列表入口返回 565 条记录。GPU 探针检测到 1 个可用设备并报告 float16 支持，已登记 CUDA 与 cuDNN 运行库搜索目录。

## 验证范围与证据

本轮包含本机启动、版本资源、GPU 能力探针、Agent 真实只读入口以及 Chromium 合成桥接交互。真实付费模型生成、GPU 转写任务、外部视频下载、真实卸载和原生 WebView2 视觉终审仍待实跑。TASK-5520 的用户交互与审美验收继续由其任务记录承接。

原始证据位于 `.ui-audit/optimization-20261117/`，包括 `integration-final-pytest.log`、`integration-js.log`、`integration-agent.log`、`integration-ui.log`、`integration-ui-matrix/coverage.json`、`benchmark-before.json`、`benchmark-after.json`、`installed-before.json`、`installed-after.json`、`installed-after-repeat.json`、`installed-runtime.json`、`installed-distribution-parity.json` 和 `installed-gpu-probe.log`。安装备份及最终清单位于同目录的 `local-install-20261118/`。
