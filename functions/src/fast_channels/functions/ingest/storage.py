"""Object access for the pipeline steps that read and rewrite encoder outputs.

The steps work against :class:`Store`, so their logic is tested with an in-memory store and the S3 adapter on its
own (botocore Stubber).
"""

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final, Protocol

from botocore.exceptions import ClientError

from fast_channels.functions import aws

CONTENT_TYPES: Final = {
    ".m3u8": "application/vnd.apple.mpegurl",  # RFC 8216 §4
    ".mpd": "application/dash+xml",
    ".cmfv": "video/mp4",
    ".cmfa": "audio/mp4",
    ".mp4": "video/mp4",
    ".xml": "application/xml",
    ".gif": "image/gif",
}
_MISSING: Final = frozenset({"404", "NoSuchKey", "NotFound"})


def content_type(key: str) -> str:
    """MIME type of an object by its extension (``binary/octet-stream`` when unknown)."""
    return CONTENT_TYPES.get(PurePosixPath(key).suffix.lower(), "binary/octet-stream")


class Store(Protocol):
    """Objects of one bucket, addressed by key."""

    def read(self, key: str) -> bytes | None:
        """The object's bytes, or ``None`` when it does not exist."""
        ...

    def exists(self, key: str) -> bool: ...

    def write(self, key: str, body: bytes, *, cache_control: str | None = None) -> None:
        """Create or replace the object, typed by its extension."""
        ...


def read_text(store: Store, key: str) -> str | None:
    """A UTF-8 object (playlist, manifest) as text, or ``None`` when it does not exist."""
    body = store.read(key)
    return None if body is None else body.decode()


@dataclass(frozen=True, slots=True)
class S3Store:
    """:class:`Store` over an S3 bucket. Callers need ``s3:ListBucket`` for missing keys to read as missing
    (S3 answers 403 instead of 404 without it)."""

    bucket: str

    def read(self, key: str) -> bytes | None:
        try:
            response = aws.client("s3").get_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if _missing(error):
                return None
            raise
        body: bytes = response["Body"].read()
        return body

    def exists(self, key: str) -> bool:
        try:
            aws.client("s3").head_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if _missing(error):
                return False
            raise
        return True

    def write(self, key: str, body: bytes, *, cache_control: str | None = None) -> None:
        extra = {"CacheControl": cache_control} if cache_control else {}
        aws.client("s3").put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type(key), **extra)


def _missing(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") in _MISSING
