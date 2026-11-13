# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
# PyInstaller spec — worker EXE（队列 worker：claim → run_engine.run → save_manifest）
# Worker 独立打包 faster-whisper / CTranslate2 / OpenCC / FFmpeg。
#
# 用法:
#   pyinstaller --noconfirm build/worker.spec
# 说明:
#   - torch 不需要；GPU/CPU 均由 CTranslate2 推理。
#   - ffmpeg 二进制来自 imageio-ffmpeg（运行时解析），随包附带。
#   - run_engine 由 worker 在 _run_job 中延迟 import，PyInstaller 静态分析可能
#     漏掉，故加入 hiddenimports 强制收集（连带 engine.* / faster_whisper / 等）。
import os
import sys

block_cipher = None

# SPECPATH 是 spec 文件所在目录（build/）；项目根 = 上一级。
ROOT = os.path.dirname(SPECPATH)

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs  # noqa: E402

binaries = []

# A1（先生 2026-10-08 瘦身）：不再把 imageio-ffmpeg 的二进制额外复制到
# _internal 根（与包内 binaries/ 副本重复，84MB 解压体积）。包内副本由
# hiddenimports 收集；get_ffmpeg 的 worker/_internal glob 与冻结态
# imageio_ffmpeg.get_ffmpeg_exe() 都能解析到它，行为不变。

# A2（先生 2026-10-08 拆包）：NVIDIA CUDA/cuDNN 运行库（解压约 2.0GB，
# 约占安装器压缩体积七成）默认不再随包——安装器只含核心刚需程序，GPU
# 用户在「准备」页自选安装 GPU 加速包（解压回 _internal/nvidia 即被
# engine/dll.py 自动发现）。SHENGJIAN_BUNDLE_CUDA=1 时仍出全量包
# （本地诊断/一次性全量分发用）。无库机器由既有守卫回退 CPU int8。
BUNDLE_CUDA = os.environ.get("SHENGJIAN_BUNDLE_CUDA", "") == "1"
if BUNDLE_CUDA:
    for package in ("nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_nvrtc"):
        try:
            binaries += collect_dynamic_libs(package)
        except Exception:
            pass

# faster-whisper 的 assets/ 内含 silero_vad*.onnx（VAD 模型），属包内数据，
# PyInstaller 默认不收集，必须显式打入，否则打包后运行报 NO_SUCHFILE。
datas = collect_data_files("faster_whisper") + [(os.path.join(ROOT, "app", "skills"), "app/skills")]

a = Analysis(
    [os.path.join(ROOT, "app", "worker.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        # worker 自身依赖
        "engine", "engine.contracts", "engine.validate", "engine.queue_store",
        # worker 在 _run_job 里延迟 import 的引擎入口（关键：必须显式收集）
        "run_engine",
        # run_engine 间接依赖
        "engine.asr", "engine.audio", "engine.text", "engine.dll",
        "engine.agent_client", "app.agent_service",
        "faster_whisper", "ctranslate2", "av", "onnxruntime",
        "opencc", "imageio_ffmpeg",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "torch", "tensorflow", "numba",
        # 拆包模式下连自动依赖分析也不得把 NVIDIA 库带回来（bindepend 会
        # 顺着 ctranslate2 的 DLL 依赖找进 site-packages/nvidia）。
        *(() if BUNDLE_CUDA else ("nvidia", "nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_nvrtc")),
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FoilPage-Worker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    version=os.path.join(ROOT, "build", "version_info_worker.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    name="FoilPage-Worker",
)
