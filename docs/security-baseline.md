# OSPS Baseline Level 1 证据

核对日期：2026-11-13。依据：[OpenSSF OSPS Baseline v2026.08.28](https://baseline.openssf.org/versions/2026-08-28.html)。范围为 [FoilPage-source](project-scope.md) 的公开源码维护和源码发行，相关旧安装包、官网和托管服务单独披露。下表是维护者自评依据，不是独立审计。

| 控制 | 证据 |
| --- | --- |
| OSPS-AC-01.01 | 唯一管理者账号的 GitHub 双重认证已启用；本轮由维护者设置页面核实。 |
| OSPS-AC-02.01 | 当前协作者仅仓库所有者；新增协作者由所有者逐一邀请和分配权限。 |
| OSPS-AC-03.01 | main 保护要求 PR 和 windows-python/frontend/agent 三项检查，strict 与 enforce_admins 启用，禁止强推；当前单维护者无需额外批准人，不表示多人审查。 |
| OSPS-AC-03.02 | main 的 allow_deletions=false。 |
| OSPS-BR-01.01 | CI 执行固定命令；源码发布输入通过环境变量传递，Python 用完整数字版号校验并以参数数组调用 gh，事件元数据不插入 shell。 |
| OSPS-BR-01.03 | 默认 Actions contents:read，checkout 不保留凭据；只有验证成功后的发布 job 使用临时 contents:write token。无托管服务或模型密钥注入构建。 |
| OSPS-BR-03.01 | 权威仓库、文档及官方发布入口使用 HTTPS。应用的 HTTP 回环接口仅用于本机通信。 |
| OSPS-BR-03.02 | GitHub 源码归档及依赖下载使用 HTTPS。历史发行入口的关系见项目范围。 |
| OSPS-BR-07.01 | GitHub secret scanning 与 push protection 启用；2026-11-13 回读告警为空。build/check_source.py 提供额外的有限模式检查；不宣称所有秘密均可自动识别。 |
| OSPS-DO-01.01 | [使用指南](user-guide.md) 覆盖准备、导入、下载、转写、整理、导出、资料与联网边界；README 提供运行/构建说明。 |
| OSPS-DO-02.01 | [缺陷报告说明](bug-reporting.md) 与 Issue 表单包含版本、复现、实际结果及隐私提醒。 |
| OSPS-GV-02.01 | 公开 GitHub Issues 和 Pull Requests 提供用户及贡献者讨论渠道。 |
| OSPS-GV-03.01 | [CONTRIBUTING.md](../CONTRIBUTING.md) 说明贡献流程、测试与第三方归属。 |
| OSPS-LE-02.01 | 自有源码使用 OSI 批准的 MPL-2.0，第三方原始许可保留。 |
| OSPS-LE-02.02 | 本源码项目 v0.1.0 Release 仅提供许可覆盖的源码归档，没有附加安装器。历史二进制不纳入本自评，详见项目范围；未来二进制发行需重新审核。 |
| OSPS-LE-03.01 | 根目录 LICENSE 包含完整 MPL-2.0 正文及 Exhibit A。 |
| OSPS-LE-03.02 | 已发布 v0.1.0 标签包含 LICENSE 与 THIRD_PARTY_NOTICES.md；GitHub 归档包含标签树内容。 |
| OSPS-QA-01.01 | 权威源码位于稳定公开的 GitHub URL。 |
| OSPS-QA-01.02 | 初始公开快照及后续 Git 历史可查看作者、日期与修改；先前私有开发历史未导入，见项目范围。 |
| OSPS-QA-02.01 | requirements-base/agent.lock.txt、requirements-build/agent-build.txt 与 package.json/package-lock.json 记录直接依赖和锁定结果。 |
| OSPS-QA-04.01 | [项目范围](project-scope.md) 列出当前源码、旧发行库、私有开发/托管库与官网的用途及发行关系。 |
| OSPS-QA-05.01 | 跟踪源码树没有应用可执行程序、安装器、动态库或字节码缓存；打包输出被忽略。 |
| OSPS-QA-05.02 | 跟踪的二进制为字体、图标、提示音与合成测试视频，来源和许可见 [第三方说明](../THIRD_PARTY_NOTICES.md)；不含编译的应用或依赖库。 |
| OSPS-VM-02.01 | [SECURITY.md](../SECURITY.md) 指向已启用的 GitHub 私密漏洞报告，并说明需要的脱敏材料。 |

GitHub 安全与访问设置由平台管理，部分依据只有维护者可读。本表不公开凭据、身份验证细节或用户数据。获得公开自评徽章后，应继续维护这些控制；新增发行方式或改变项目范围时需重新核验。
