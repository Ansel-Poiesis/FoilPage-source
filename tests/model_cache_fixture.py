# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Small structurally valid cache records; no real inference weights or network."""
import json
import struct
from pathlib import Path


def model_binary(*, version=6, dtype=0, elements=4, aliases=()):
    def text(value):
        encoded = value.encode("utf-8") + b"\0"
        return struct.pack("<H", len(encoded)) + encoded
    width = (4, 1, 2, 4, 2, 2)[dtype]
    header = struct.pack("<I", version) + text("WhisperSpec") + struct.pack("<II", 3, 1)
    tensor = text("encoder/conv1/weight") + struct.pack("<BI", 1, elements)
    tensor += struct.pack("<BI", dtype if version >= 4 else width, elements * width if version >= 4 else elements)
    tensor += b"\0" * (elements * width)
    alias_records = b"" if version < 3 else struct.pack("<I", len(aliases)) + b"".join(text(a) + text(b) for a, b in aliases)
    return header + tensor + alias_records


def write_model_snapshot(snapshot: Path, *, version=6, dtype=0, vocabulary="txt"):
    snapshot.mkdir(parents=True, exist_ok=True)
    (snapshot / "config.json").write_text("{}", encoding="utf-8")
    (snapshot / "model.bin").write_bytes(model_binary(version=version, dtype=dtype))
    tokenizer = {"version":"1.0","truncation":None,"padding":None,"added_tokens":[],
                 "normalizer":None,"pre_tokenizer":None,"post_processor":None,"decoder":None,
                 "model":{"type":"WordLevel","vocab":{"[UNK]":0},"unk_token":"[UNK]"}}
    (snapshot / "tokenizer.json").write_text(json.dumps(tokenizer), encoding="utf-8")
    (snapshot / ("vocabulary.json" if vocabulary == "json" else "vocabulary.txt")).write_text(
        json.dumps(["[UNK]"]) if vocabulary == "json" else "[UNK]\n", encoding="utf-8")
    return snapshot


def create_model_cache(root: Path, name="tiny", *, layout="direct", **kwargs):
    hub = root / "hub" if layout == "legacy" else root
    repo = hub / ("models--Systran--faster-whisper-" + name)
    (repo / "refs").mkdir(parents=True, exist_ok=True)
    (repo / "refs" / "main").write_text("fixture", encoding="utf-8")
    return write_model_snapshot(repo / "snapshots" / "fixture", **kwargs)
