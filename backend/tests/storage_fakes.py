"""存储适配器 contract suite 使用的 SDK fake（内存实现）。

- FakeS3Client：模拟 boto3 s3 client 被适配器使用的方法面（含 ClientError 映射路径）；
- FakeOssClient：模拟 alibabacloud-oss-v2 client（接收 Request 对象，异常带 status_code）；
- 仅实现适配器实际调用的 API；fake 与适配器的偏差会在契约测试中暴露。

注意：fake 注入仅锁定适配器语义；真实云链路（凭据/网络/版本行为）仍待独立验收。
"""

from __future__ import annotations

import hashlib
import io
from types import SimpleNamespace
from typing import Any


def _weak_etag(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _parse_range(range_header: str, size: int) -> slice:
    start_text, end_text = range_header.removeprefix("bytes=").split("-")
    start = int(start_text)
    end = int(end_text) if end_text else size - 1
    return slice(start, end + 1)


def _read_body(body: Any) -> bytes:
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    return body.read()


class FakeS3Client:
    """boto3 s3 client 替身：内存对象与分片会话。"""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self._upload_seq = 0

    def _client_error(self, code: str, status: int, operation: str) -> Exception:
        from botocore.exceptions import ClientError

        return ClientError(
            {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
            operation,
        )

    # -- 基础操作 -------------------------------------------------------

    def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None):  # noqa: N803
        self.calls.append("upload_fileobj")
        data = fileobj.read()
        self.objects[(bucket, key)] = data
        return None

    def head_object(self, Bucket, Key):  # noqa: N803
        self.calls.append("head_object")
        data = self.objects.get((Bucket, Key))
        if data is None:
            raise self._client_error("404", 404, "HeadObject")
        return {
            "ContentLength": len(data),
            "ETag": f'"{_weak_etag(data)}"',
            "ContentType": "application/octet-stream",
        }

    def get_object(self, Bucket, Key, Range=None):  # noqa: N803
        self.calls.append("get_object")
        data = self.objects.get((Bucket, Key))
        if data is None:
            raise self._client_error("NoSuchKey", 404, "GetObject")
        if Range is not None:
            data = data[_parse_range(Range, len(data))]
        return {"Body": io.BytesIO(data)}

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.calls.append("delete_object")
        self.objects.pop((Bucket, Key), None)
        return {}

    def generate_presigned_url(self, operation, Params=None, ExpiresIn=None):  # noqa: N803
        params = Params or {}
        return f"https://fake-s3.local/{params.get('Bucket')}/{params.get('Key')}?expires={ExpiresIn}"

    # -- multipart ------------------------------------------------------

    def create_multipart_upload(self, Bucket, Key):  # noqa: N803
        self._upload_seq += 1
        upload_id = f"fake-upload-{self._upload_seq}"
        self.uploads[upload_id] = {"bucket": Bucket, "key": Key, "parts": {}}
        return {"UploadId": upload_id}

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body):  # noqa: N803
        upload = self.uploads.get(UploadId)
        if upload is None:
            raise self._client_error("NoSuchUpload", 404, "UploadPart")
        upload["parts"][PartNumber] = _read_body(Body)
        return {"ETag": f'"part-{PartNumber}-etag"'}

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):  # noqa: N803
        upload = self.uploads.pop(UploadId, None)
        if upload is None:
            raise self._client_error("NoSuchUpload", 404, "CompleteMultipartUpload")
        parts = sorted(upload["parts"].items())
        self.objects[(Bucket, Key)] = b"".join(chunk for _, chunk in parts)
        return {"ETag": '"complete-etag"'}

    def abort_multipart_upload(self, Bucket, Key, UploadId):  # noqa: N803
        self.uploads.pop(UploadId, None)
        return {}


class FakeOssError(Exception):
    """模拟 OSS 服务错误：带 status_code/code 供适配器 _map_error 判定。"""

    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(f"oss error {status_code}/{code}")
        self.status_code = status_code
        self.code = code


class FakeOssClient:
    """alibabacloud-oss-v2 client 替身：接收 Request 对象。"""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self._upload_seq = 0

    def put_object(self, request: Any) -> Any:
        self.objects[(request.bucket, request.key)] = _read_body(request.body)
        return SimpleNamespace(etag="fake-oss-etag")

    def head_object(self, request: Any) -> Any:
        data = self.objects.get((request.bucket, request.key))
        if data is None:
            raise FakeOssError(404, "NoSuchKey")
        return SimpleNamespace(
            content_length=len(data),
            etag="fake-oss-etag",
            version_id=None,
            content_type="application/octet-stream",
        )

    def get_object(self, request: Any) -> Any:
        data = self.objects.get((request.bucket, request.key))
        if data is None:
            raise FakeOssError(404, "NoSuchKey")
        byte_range = getattr(request, "range", None)
        if byte_range:
            data = data[_parse_range(byte_range, len(data))]
        return SimpleNamespace(body=io.BytesIO(data))

    def delete_object(self, request: Any) -> Any:
        self.objects.pop((request.bucket, request.key), None)
        return SimpleNamespace(status_code=204)

    def presign(self, request: Any, expires: Any = None) -> Any:
        return SimpleNamespace(url=f"https://fake-oss.local/{request.bucket}/{request.key}")

    # -- multipart ------------------------------------------------------

    def initiate_multipart_upload(self, request: Any) -> Any:
        self._upload_seq += 1
        upload_id = f"fake-oss-upload-{self._upload_seq}"
        self.uploads[upload_id] = {"bucket": request.bucket, "key": request.key, "parts": {}}
        return SimpleNamespace(upload_id=upload_id)

    def upload_part(self, request: Any) -> Any:
        upload = self.uploads.get(request.upload_id)
        if upload is None:
            raise FakeOssError(404, "NoSuchUpload")
        upload["parts"][request.part_number] = _read_body(request.body)
        return SimpleNamespace(etag=f'"part-{request.part_number}-etag"')

    def complete_multipart_upload(self, request: Any) -> Any:
        upload = self.uploads.pop(request.upload_id, None)
        if upload is None:
            raise FakeOssError(404, "NoSuchUpload")
        parts = sorted(request.complete_multipart_upload.parts, key=lambda part: part.part_number)
        self.objects[(request.bucket, request.key)] = b"".join(
            upload["parts"][part.part_number] for part in parts
        )
        return SimpleNamespace(etag='"complete-etag"')

    def abort_multipart_upload(self, request: Any) -> Any:
        self.uploads.pop(request.upload_id, None)
        return SimpleNamespace(status_code=204)
