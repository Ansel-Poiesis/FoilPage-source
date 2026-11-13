# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import base64
import http.client
from urllib.parse import urlsplit

import pytest

from app.media_preview import MediaPreviewServer, byte_range
from engine.media_store import MediaStore


@pytest.mark.parametrize("header,size,expected", [("bytes=2-5",10,(2,5)), ("bytes=5-",10,(5,9)),
    ("bytes=-3",10,(7,9)), ("bytes=0-1000",10,(0,9)), ("bytes=-100",10,(0,9))])
def test_seek_ranges(header, size, expected):
    assert byte_range(header, size) == expected


@pytest.mark.parametrize("header", ["bytes=10-", "bytes=5-2", "bytes=-0", "bytes=-", "bytes=0-1,5-6", "items=0-1"])
def test_unsatisfiable_or_multiple_ranges(header):
    with pytest.raises(ValueError):
        byte_range(header, 10)


def request(url, method="GET", **headers):
    parsed = urlsplit(url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    try:
        connection.request(method, parsed.path + "?" + parsed.query, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_only_registered_video_capability_can_stream_seek_and_thumbnail(tmp_path, monkeypatch):
    media = MediaStore(tmp_path / "library.db")
    source = tmp_path / "video.mp4"
    source.write_bytes(b"0123456789")
    item = media.import_file(str(source)).item
    server = MediaPreviewServer(media, tmp_path / "cache")
    calls = []
    monkeypatch.setattr("app.media_preview.extract_frame_data_urls", lambda *a, **k: calls.append(a) or [
        {"data_url": "data:image/jpeg;base64," + base64.b64encode(b"fixture-jpeg").decode()}])
    try:
        urls = server.urls(item["id"])
        assert str(tmp_path) not in str(urls)
        assert request(urls["preview_url"])[2] == b"0123456789"
        code, headers, body = request(urls["preview_url"], Range="bytes=2-5")
        assert code == 206 and body == b"2345" and headers["Content-Range"] == "bytes 2-5/10"
        assert request(urls["preview_url"], "HEAD", Range="bytes=-3")[2] == b""
        assert request(urls["preview_url"], Range="bytes=100-")[0] == 416
        assert request(urls["preview_url"].split("?")[0])[0] == 403
        assert request(urls["preview_url"], Host="attacker.test")[0] == 403
        assert request(urls["preview_url"].replace(item["id"], "f" * 32))[0] == 404
        for _ in range(2):
            assert request(urls["thumbnail_url"])[2] == b"fixture-jpeg"
        assert len(calls) == 1
        source.unlink()
        assert request(urls["preview_url"])[0] == 404
    finally:
        server.stop()
        media.close()


def test_audio_and_arbitrary_path_never_get_a_video_capability(tmp_path):
    media = MediaStore(tmp_path / "library.db")
    source = tmp_path / "sample.wav"
    source.write_bytes(b"audio")
    item = media.import_file(str(source)).item
    server = MediaPreviewServer(media, tmp_path / "cache")
    try:
        for candidate in (item["id"], "../../secret", str(source)):
            with pytest.raises(FileNotFoundError):
                server.urls(candidate)
        assert server._server is None
    finally:
        server.stop()
        media.close()
