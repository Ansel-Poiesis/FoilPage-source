# 任务选中层次、勾选动效与快捷取消

任务 TASK-5520.1。先生反馈“选中某一栏的效果太扁平了”，要求改进打勾动画、单击选中和取消等快捷操作。承接 62f89a9 的操作分组与署名设计，验收沿用先生指定的截图方式。

## 前端开工

### 代表任务

在长任务列表中点选一项查看详情，再次点选取消；用复选框、Ctrl 和 Shift 选择多项，随时清空并继续浏览原位置。

### 本次变化

任务行和侧栏导航使用带柔和光影的选中表面，复选框采用短促的描勾与撤销过渡。补充重复单击取消、列表全选及清空快捷键，右侧继续承接批量动作。

### 已有条件

[workbench.js](C:/Ansel_Work/B-软件开发/FoilPage-视频的二向箔/FoilPage-source/app/webview_ui/js/workbench.js) 已有稳定任务身份、Shift 范围选择、原生复选框及就地更新选中态。[product.css](C:/Ansel_Work/B-软件开发/FoilPage-视频的二向箔/FoilPage-source/app/webview_ui/css/product.css) 提供十个主题、键盘焦点、减少动态和高对比规则。已有设计移除了高亮内边线，本轮保留这一决定，通过表面光影和勾选反馈增强层次。

采用 frontend-experience 的局部改进流程、emil-design-eng 的短时可打断反馈，以及本机 Modern Web Guidance 的 checkbox 指南。复选框保留原生语义、标签和键盘行为；列表选择继续保存在 AppState.selected，不调用写入接口。

### 验证办法

用合成任务验证单击切换、复选框标签、Ctrl/Shift 选择、Ctrl+A、Esc、过滤与推送更新后的身份及滚动。核对快速反复点击、键盘即时反馈、减少动态和高对比。按主题和窗口生成选中、取消、多选及动效阶段截图，审看后同步本机前端资源。

## 体验方案

| Before | After | Why |
| --- | --- | --- |
| 选中状态只有平涂底色 | 柔和渐变、短阴影与明确的勾选控件 | 在现有主题中增加表面层次 |
| 原生勾号瞬间出现 | 描勾与轻微回弹，取消时反向收回 | 让操作结果容易感知，连续点击可打断 |
| 已选行重复单击仍保持选中 | 再次单击取消该行 | 快速撤销选择 |
| 清空依赖逐项点击 | Esc 清空，Ctrl+A 全选当前列表，右侧提供取消选择 | 减少多项处理前后的操作 |

鼠标勾选反馈控制在 200ms 内。键盘和减少动态使用即时状态；数据推送和页面返回只恢复真实选择，不补播操作动画。快捷键在任务列表或详情区域生效，编辑输入和模态框保留各自行为。

### 任务路径

单击未选行进行单选，再次单击已选行取消该行。复选框和 Ctrl/Meta 保留其他选择；Shift 延伸范围。列表获得焦点后 Ctrl/Meta+A 全选当前过滤结果，Esc 清空；右侧多选面板提供“取消选择”。

### 页面职责

[任务工作台](C:/Ansel_Work/B-软件开发/FoilPage-视频的二向箔/FoilPage-source/app/webview_ui/js/workbench.js) 统一处理行、复选框与快捷键，右侧显示详情和批量动作。[产品样式](C:/Ansel_Work/B-软件开发/FoilPage-视频的二向箔/FoilPage-source/app/webview_ui/css/product.css) 提供各主题的浮起表面和描勾动效，延续现有单项任务的七个分组动作与侧栏署名。

### 状态与验证

无选择、单选、多选、清空、范围选择、过滤结果、长列表位置、键盘、减少动态和强制色均纳入检查。修改通过原生 checked、AppState.selected 与 aria-pressed 三者一致性验证；选择改变时不重新建立列表节点。

## 工程实现

### 实现与复用

[workbench.js](C:/Ansel_Work/B-软件开发/FoilPage-视频的二向箔/FoilPage-source/app/webview_ui/js/workbench.js) 复用 selectTask、Shift 范围基线、dataKey 和 syncSelection，增加重复单击取消、清空与限定作用域的全选快捷键。checkbox 保留真实 input 和 label，仅增加不可交互的 SVG 勾号。 [product.css](C:/Ansel_Work/B-软件开发/FoilPage-视频的二向箔/FoilPage-source/app/webview_ui/css/product.css) 增加柔和渐变和外阴影，勾选 180ms、回弹 190ms、取消 110ms，44×44 标签点击区。键盘与减少动态立即反馈，强制色恢复原生复选框。

### 数据与状态

选择仍由稳定 task_id 集合管理；单击已选行只移除该行，Ctrl/Meta 与复选框追加切换，Shift 维持已有范围规则。清空保留列表、滚动与行焦点，批量按钮被移除时将焦点放回原锚点。表单和模态框继续使用自身快捷键。未添加后端接口或持久化字段。

### 工程检查

[专项回归](C:/Ansel_Work/B-软件开发/FoilPage-视频的二向箔/FoilPage-source/tests/test_task_selection.mjs) 的 7 项覆盖重复点击、标签转发、Ctrl、过滤后全选、Esc、60 行列表滚动与焦点、快速连续操作、键盘/减少动态/强制色和模态编辑。与桌面交互、工作流、进度及主题对比度共 62 项通过；npm run test:ui 的 4 组通过。build/check_source.py、build/version.py --check、JavaScript 语法与 Git 差异格式检查通过。

首次键盘检查发现 checked 选择器优先级与删除按钮的焦点渐变仍会创建动画，现对键盘选择模式下的行及子节点统一关闭 transition，回归后即时反馈成立。

## 目标环境验收

### 环境与范围

按先生指定采用截图验收，未执行桌面输入操作。[截图索引](C:/Ansel_Work/B-软件开发/FoilPage-视频的二向箔/FoilPage-source/.ui-audit/task-5520.1/acceptance/index.html) 基于真实前端和合成桥接数据，在 Headless Chromium 中覆盖 10 个主题、1280×800 与 560×500/130% 字号，深浅主题另含 1272×773/115% 字号/墨色强调和 960×800。共 78 张状态截图；另按 0、35、70、110、160、240ms 采集深浅两套勾选/取消的 24 张阶段图。捕获前后 30 份前端源码 SHA256 保持一致。

### 任务结果

十套主题的选中、取消与多选可读，选中表面的外阴影和渐变能区分列表层次，未恢复高亮内边线。描勾、收回和轻微回弹按真实状态收敛；键盘、减少动态和强制色保留即时反馈。所有截图的列表/详情/过滤区无横向溢出、无未命名可见控件、复选框原生状态与行状态一致，标签点击区均为 44×44。紧凑多选面板的统计和操作区域不再互相遮挡。

逐主题总览、深浅主题的完整窗口、紧凑多选面板和两套阶段图均已审看。 [原始结果](C:/Ansel_Work/B-软件开发/FoilPage-视频的二向箔/FoilPage-source/.ui-audit/task-5520.1/acceptance/results.json) 保存每张截图的检查项与源码指纹，索引可按主题、尺寸和状态筛选，动图可暂停并遵循减少动态设置。

### 问题与结论

第一轮 560×500 截图暴露出快捷键提示挤压批量统计，底部固定动作遮住部分内容。已在紧凑布局中将三项统计并排展示并减少提示外边距；第二轮 78 张状态和 24 张阶段图重采集通过，追加统计区域遮挡检查。62 项相关测试与 4 组 UI 检查也在该修正后重新通过。

## 交付与回流

### 交付入口

代码入口为 [workbench.js](C:/Ansel_Work/B-软件开发/FoilPage-视频的二向箔/FoilPage-source/app/webview_ui/js/workbench.js) 与 [product.css](C:/Ansel_Work/B-软件开发/FoilPage-视频的二向箔/FoilPage-source/app/webview_ui/css/product.css)，[截图与动图画廊](C:/Ansel_Work/B-软件开发/FoilPage-视频的二向箔/FoilPage-source/.ui-audit/task-5520.1/acceptance/index.html) 可直接复核。本地同步收据和原文件备份位于 [.ui-audit/task-5520.1/local-install.json](C:/Ansel_Work/B-软件开发/FoilPage-视频的二向箔/FoilPage-source/.ui-audit/task-5520.1/local-install.json)。

### 使用与状态

已同步本机 `C:/Users/mingc/AppData/Local/Programs/FoilPage/_internal` 下的两份前端文件；覆盖前确认全部 30 份安装资源与当前基线一致，保存原件并原子替换，覆盖后 30 份资源 SHA256 与本次截图源码一致。任务队列空闲时正常退出、重启 FoilPage；只读健康检查返回 502 项任务、422 项完成、0 项活动，窗口启用，Worker 运行。版本沿用 0.1.5。

使用方式：单击未选行单选，再点已选行取消该行；勾选或 Ctrl/Meta 追加切换，Shift 连选；列表中 Ctrl/Meta+A 全选当前过滤结果，Esc 或“取消选择”清空。清空后继续停留在原列表位置。

### 经验更新

选中状态继续就地更新列表与右侧动作，原生复选框作为唯一可操作控件；动画只负责反馈。优先级检查要覆盖 checked 选择器和既有子控件过渡；加入帮助文案后必须重看紧凑窗口的统计与固定动作间距。经验随本项目记录保存，未修改主记忆。
