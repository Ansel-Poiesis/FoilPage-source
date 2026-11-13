# 项目范围与发行渠道

[FoilPage-source](https://github.com/Ansel-Poiesis/FoilPage-source) 是当前桌面端、引擎、浏览器扩展、Agent、测试与构建代码的唯一权威公开代码库。0.1.0 开始新的源码版本序列，自有源码采用 MPL-2.0，第三方保留原许可。公开 Git 历史从经过清理的初始快照开始，记录后续作者、时间和差异，不包含此前完整开发历史。

本仓库 [Releases](https://github.com/Ansel-Poiesis/FoilPage-source/releases) 当前提供带 LICENSE 的源码归档。v0.1.0 没有应用安装器、GPU 包或编译库附件。字体、图标、提示音与合成测试视频为内容素材，见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。未来二进制发行需重新核对完整依赖、许可与对应源码材料。

| 相关代码库与服务 | 用途与状态 |
| --- | --- |
| [FoilPage-releases](https://github.com/Ansel-Poiesis/FoilPage-releases) | 旧版安装器、GPU 包、指南及历史入口，沿用独立 1.x 版本序列。历史二进制未通过本轮源码项目的完整许可与供应链审核。 |
| Ansel-Poiesis/FoilPage（私有） | 源码公开前的开发历史、内部运行证据与独立运营的账户、计费等服务实现。当前客户端开发已迁至本公开仓库。 |
| [FoilPage 官网](https://foilpage.early-snipe-7202.chatgpt.site) | 产品介绍、旧安装版入口与浏览器工作区，其网页及账户/媒体代理代码使用独立发布流程。 |

上述相关代码库均未编译进本仓库 v0.1.0 源码归档。官方托管服务是外部运行依赖；源码授权不授予运营资源、账户额度或商标权。

OpenSSF OSPS Baseline 自评针对 FoilPage-source 公开源码的维护和源码发行流程，不覆盖历史 1.x 二进制、官网、托管服务、用户自行构建的产物或用户设备。项目关系在此披露，不能据此推导整个 FoilPage 产品家族已经过独立安全审计。

检查包括保护分支、只读 CI、依赖清单、秘密扫描、私密漏洞通道和源码发行材料。徽章属于维护者在指定日期和标准版本下的公开自评，不能保证软件没有漏洞。
