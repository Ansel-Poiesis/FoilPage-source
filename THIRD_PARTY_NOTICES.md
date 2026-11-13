# 第三方组件

FoilPage 自有源码采用 MPL-2.0。第三方代码、依赖、字体、模型和二进制按各自原始许可分发。

| 仓库内组件 | 材料位置 | 许可 |
| --- | --- | --- |
| MicroModal 0.7.0 | app/webview_ui/vendor/micromodal.min.js 和 micromodal-LICENSE.txt | MIT |
| LXGW WenKai Screen 字体 | assets/fonts/OFL.txt，browser-extension/fonts | SIL OFL 1.1 |
| FFmpeg 许可及来源索引 | assets/third-party | GPLv3 原文及二进制来源索引 |

assets/sample.mp4 由黑色画面与 Windows System.Speech 合成英文语音组成，文本为“Today we will test the video transcription pipeline. The moon is bright above the quiet ocean.”。图标和提示音的自有素材随本项目提供。

运行依赖固定在 requirements-base.lock.txt，Agent 依赖固定在 requirements-agent.lock.txt，Node 开发依赖固定在 package-lock.json。依赖安装后，其发行元数据和原始许可随相应包提供。

主要组件包括 faster-whisper、CTranslate2、PyAV、imageio-ffmpeg、OpenCC、httpx、yt-dlp、Pillow、pywin32、pystray、pywebview、MCP Python SDK 和 Playwright。其许可适用范围保留到各自组件。模型由用户下载，使用时遵循所选模型的许可。

## 二进制再分发

build/collect_release_notices.py 从构建环境收集原始许可和材料，为文件记录版本与 SHA256。安装包应同时提供 FoilPage 许可、对应源码入口及所需第三方材料。

FFmpeg 的 Python 封装和实际执行文件分别核对。assets/third-party/FFmpeg-SOURCES.md 记录现有二进制来源。再分发时需核对对应二进制的源码及构建依赖材料。NVIDIA CUDA/cuDNN 可选运行库按 NVIDIA 随包许可及再分发条款处理。

原始许可文件和对应源码地址应随每次二进制发布核验。
