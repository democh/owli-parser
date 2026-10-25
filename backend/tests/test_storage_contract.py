"""存储适配器 contract suite：Local / S3(fake) / OSS(fake) 语义一致性。

同一套行为断言对三个后端执行（fake client 注入，不触网）：
roundtrip、范围读、校验和不符拒绝、多分片提交/中止、缺失对象语义、
能力声明与 presign（按 capabilities 分支）。
真实云链路（凭据/网络/SDK 版本行为）不在本套件范围，另行验收。
"""

from __future__ import annotations

import hashlib
import time
import uuid

import pytest

from owli_parser.adapters.storage.aliyun_oss import AliyunOssStorageBackend
from owli_parser.adapters.storage.local import LocalStorageBackend
from owli_parser.adapters.storage.s3 import S3StorageBackend
from owli_parser.ports.storage import (
    ByteRange,
    ObjectLocator,
    ObjectTarget,
    PartReceipt,
    PutOptions,
    SignedReadOptions,
    StorageChecksumMismatch,
    StorageNotFound,
    StorageUnavailable,
)
from tests.storage_fakes import FakeOssClient, FakeS3Client

KEY = "contract/data.bin"
BUCKET = "contract-bucket"
PAYLOAD = b"owli storage contract payload | " * 3
PAYLOAD_SHA256 = hashlib.sha256(PAYLOAD).hexdigest()


@pytest.fixture(params=["local", "s3", "oss"])
def storage(request, tmp_path):
    """返回 (backend, target, locator)；三后端构造口径一致。"""
    profile = uuid.uuid4()
    if request.param == "local":
        backend = LocalStorageBackend(tmp_path / "root")
        bucket = None
    elif request.param == "s3":
        backend = S3StorageBackend(
            endpoint_url=None,
            region="us-east-1",
            bucket=BUCKET,
            addressing_style="path",
            access_key="test-key",
            secret_key="test-secret",
            client=FakeS3Client(),
        )
        bucket = BUCKET
    else:
        backend = AliyunOssStorageBackend(
            region="cn-hangzhou",
            endpoint="https://oss-contract.example",
            bucket=BUCKET,
            access_key_id="test-key",
            access_key_secret="test-secret",
            client=FakeOssClient(),
        )
        bucket = BUCKET
    target = ObjectTarget(profile_revision_id=profile, bucket=bucket, root_ref=None, key=KEY)
    locator = ObjectLocator(profile_revision_id=profile, bucket=bucket, root_ref=None, key=KEY)
    return backend, target, locator


def test_capabilities_shape(storage) -> None:
    backend, _target, _locator = storage
    caps = backend.capabilities()
    assert caps.range_read is True
    assert caps.multipart is True
    assert caps.versioning is False


def test_put_stat_read_delete_roundtrip(storage) -> None:
    backend, target, locator = storage

    # 写入（含 sha256 校验）→ 统计 → 全量读 → 范围读 → 删除 → 不存在
    stored = backend.put(
        target,
        iter([PAYLOAD[:10], PAYLOAD[10:]]),
        PutOptions(content_type="application/octet-stream"),
    )
    assert stored.size == len(PAYLOAD)
    assert stored.sha256 == PAYLOAD_SHA256
    assert stored.locator.key == KEY

    stat = backend.stat(locator)
    assert stat.exists is True
    assert stat.size == len(PAYLOAD)

    reader = backend.open_reader(locator, None)
    try:
        assert reader.read() == PAYLOAD
    finally:
        reader.close()

    ranged = backend.open_reader(locator, ByteRange(start=5, end_inclusive=15))
    try:
        assert ranged.read() == PAYLOAD[5:16]
    finally:
        ranged.close()

    deleted = backend.delete(locator)
    assert deleted.deleted is True
    assert backend.stat(locator).exists is False


def test_put_rejects_checksum_mismatch(storage) -> None:
    backend, target, _locator = storage
    with pytest.raises(StorageChecksumMismatch):
        backend.put(target, iter([PAYLOAD]), PutOptions(expected_sha256="0" * 64))
    unwritten = ObjectLocator(
        profile_revision_id=target.profile_revision_id, bucket=target.bucket, root_ref=None, key=KEY
    )
    assert backend.stat(unwritten).exists is False


def test_missing_object_semantics(storage) -> None:
    backend, _target, locator = storage
    assert backend.stat(locator).exists is False
    with pytest.raises(StorageNotFound):
        backend.open_reader(locator, None)
    # 删除不存在对象不抛错（幂等），随后仍是不存在
    backend.delete(locator)
    assert backend.stat(locator).exists is False


def test_multipart_roundtrip(storage) -> None:
    backend, target, locator = storage

    handle = backend.initiate(target)
    assert handle.part_size_bytes >= 0
    part1 = backend.upload_part(handle, 1, iter([PAYLOAD[:20]]))
    part2 = backend.upload_part(handle, 2, iter([PAYLOAD[20:]]))
    assert part1.part_no == 1 and part1.size == 20
    assert part2.part_no == 2 and part2.size == len(PAYLOAD) - 20

    completed = backend.complete(handle, [part2, part1])  # 乱序输入按 part_no 归位
    assert completed.size == len(PAYLOAD)
    # multipart 的 sha256 允许为 None（引擎 ETag 不能当 SHA-256），有效值须正确
    assert completed.sha256 in (None, PAYLOAD_SHA256)
    assert backend.stat(locator).size == len(PAYLOAD)

    reader = backend.open_reader(locator, None)
    try:
        assert reader.read() == PAYLOAD
    finally:
        reader.close()


def test_multipart_part_numbering_and_missing_part(storage) -> None:
    backend, target, _locator = storage
    handle = backend.initiate(target)
    with pytest.raises(StorageUnavailable):
        backend.upload_part(handle, 0, iter([b"x"]))
    backend.upload_part(handle, 1, iter([b"a"]))
    backend.upload_part(handle, 3, iter([b"c"]))

    with pytest.raises(StorageUnavailable):  # 编号不连续
        backend.complete(handle, [PartReceipt(part_no=1, size=1), PartReceipt(part_no=3, size=1)])
    with pytest.raises(StorageUnavailable):  # 空清单
        backend.complete(handle, [])


def test_multipart_abort_then_complete_fails(storage) -> None:
    backend, target, _target = storage
    handle = backend.initiate(target)
    backend.upload_part(handle, 1, iter([b"abort-me"]))
    backend.abort(handle)

    with pytest.raises((StorageNotFound, StorageUnavailable)):
        backend.complete(handle, [PartReceipt(part_no=1, size=8, etag=None, sha256=None)])


def test_presign_when_supported(storage) -> None:
    backend, _target, locator = storage
    if not backend.capabilities().presigned_read:
        pytest.skip("该后端经应用代理下载（presigned_read=false）")
    signed = backend.presign_read(locator, SignedReadOptions(ttl_seconds=300))
    assert signed.url.startswith("http")
    assert signed.expires_at_epoch > int(time.time())
