import io
from typing import Any

import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody

from fast_channels.functions.ingest.storage import S3Store, content_type, read_text


def _body(data: bytes) -> StreamingBody:
    return StreamingBody(io.BytesIO(data), len(data))


def test_content_types() -> None:
    assert content_type("a/main.m3u8") == "application/vnd.apple.mpegurl"
    assert content_type("a/main_avc.MPD") == "application/dash+xml"
    assert content_type("a/init.cmfv") == "video/mp4"
    assert content_type("ads/b.gif") == "image/gif"
    assert content_type("a/blob") == "binary/octet-stream"


def test_read_and_exists(stubs: Any) -> None:
    s3 = stubs["s3"]
    s3.add_response("get_object", {"Body": _body(b"#EXTM3U\n")}, {"Bucket": "media", "Key": "a/main.m3u8"})
    s3.add_client_error("get_object", "NoSuchKey", http_status_code=404)
    s3.add_response("head_object", {}, {"Bucket": "media", "Key": "a/main.m3u8"})
    s3.add_client_error("head_object", "404", http_status_code=404)
    store = S3Store("media")

    assert read_text(store, "a/main.m3u8") == "#EXTM3U\n"
    assert store.read("a/missing.m3u8") is None
    assert store.exists("a/main.m3u8")
    assert not store.exists("a/missing.m3u8")


def test_write_types_the_object(stubs: Any) -> None:
    stubs["s3"].add_response(
        "put_object",
        {},
        {
            "Bucket": "media",
            "Key": "ads/x/vast.xml",
            "Body": b"<VAST/>",
            "ContentType": "application/xml",
            "CacheControl": "max-age=300",
        },
    )
    S3Store("media").write("ads/x/vast.xml", b"<VAST/>", cache_control="max-age=300")


def test_other_errors_propagate(stubs: Any) -> None:
    stubs["s3"].add_client_error("get_object", "AccessDenied", http_status_code=403)
    with pytest.raises(ClientError):
        S3Store("media").read("a/main.m3u8")


def test_head_errors_propagate(stubs: Any) -> None:
    stubs["s3"].add_client_error("head_object", "403", http_status_code=403)
    with pytest.raises(ClientError):
        S3Store("media").exists("a/main.m3u8")
