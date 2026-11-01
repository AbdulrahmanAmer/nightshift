import hashlib
from pathlib import Path

import pytest

from ns_common.paths import Paths
from ns_dashboard.errors import ApiError
from ns_dashboard.uploads import CHUNK, MAX_CHUNK, UploadManager, find_source, source_dir


def test_chunk_size_is_8_mb():
    assert CHUNK == 8 * 1024 * 1024


def test_two_chunks_complete_with_sha256(tmp_home: Path):
    paths = Paths.from_env()
    mgr = UploadManager(paths)
    payload = bytes(range(256)) * 40  # 10,240 bytes, sent as two chunks
    info = mgr.start("C:\\Users\\me\\Videos\\Stream VOD.MP4", len(payload))
    assert info.status == "uploading" and info.offset == 0 and info.filename == "Stream VOD.MP4"
    assert info.upload_id == info.source_id and info.source_id.startswith("src-")
    info = mgr.append(info.upload_id, 0, payload[:6000])
    assert info.offset == 6000
    info = mgr.append(info.upload_id, 6000, payload[6000:])
    assert info.offset == len(payload) and info.status == "uploading"
    done = mgr.complete(info.upload_id)
    assert done.status == "complete" and done.sha256 == hashlib.sha256(payload).hexdigest()
    final = Path(done.path or "")
    assert final.name == "source.mp4" and final.read_bytes() == payload
    assert final.parent == paths.home / "imports" / "clipper" / info.source_id
    assert not list(final.parent.glob("*.part"))
    assert find_source(paths, info.source_id) == final
    assert mgr.complete(info.upload_id).sha256 == done.sha256  # idempotent
    with pytest.raises(ApiError) as ei:
        mgr.append(info.upload_id, len(payload), b"x")
    assert ei.value.code == "already_complete"


def test_resume_after_dropped_chunk(tmp_home: Path):
    mgr = UploadManager(Paths.from_env())
    info = mgr.start("a.mkv", 100)
    mgr.append(info.upload_id, 0, b"a" * 40)
    # the client lost the response to its first PUT and retries from a stale offset
    with pytest.raises(ApiError) as ei:
        mgr.append(info.upload_id, 0, b"a" * 40)
    assert ei.value.status == 409 and ei.value.code == "offset_mismatch"
    assert ei.value.to_body()["error"]["details"] == {"offset": 40}
    resume = mgr.status(info.upload_id)
    assert resume.offset == 40
    mgr.append(info.upload_id, resume.offset, b"b" * 60)
    assert mgr.complete(info.upload_id).size == 100


def test_complete_requires_all_bytes(tmp_home: Path):
    mgr = UploadManager(Paths.from_env())
    info = mgr.start("a.mov", 10)
    mgr.append(info.upload_id, 0, b"12345")
    with pytest.raises(ApiError) as ei:
        mgr.complete(info.upload_id)
    assert ei.value.code == "incomplete" and ei.value.to_body()["error"]["details"] == {"offset": 5}
    with pytest.raises(ApiError) as ei2:
        mgr.append(info.upload_id, 5, b"x" * 6)
    assert ei2.value.code == "too_much_data"


def test_validation_errors(tmp_home: Path):
    mgr = UploadManager(Paths.from_env())
    for bad_name in ("notes.txt", "virus.exe", "noext", ""):
        with pytest.raises(ApiError) as ei:
            mgr.start(bad_name, 10)
        assert ei.value.code == "bad_file_type"
    for bad_size in (0, -5):
        with pytest.raises(ApiError) as ei:
            mgr.start("a.mp4", bad_size)
        assert ei.value.code == "bad_size"
    info = mgr.start("a.mp4", 10)
    with pytest.raises(ApiError) as ei:
        mgr.append(info.upload_id, 0, b"")
    assert ei.value.code == "empty_chunk"
    with pytest.raises(ApiError) as ei:
        mgr.append(info.upload_id, 0, b"x" * (MAX_CHUNK + 1))
    assert ei.value.code == "chunk_too_large"


def test_ids_cannot_escape_the_imports_folder(tmp_home: Path):
    paths = Paths.from_env()
    mgr = UploadManager(paths)
    for bad in ("..", "../../config", "src-1", "C:\\Windows"):
        with pytest.raises(ApiError) as ei:
            mgr.status(bad)
        assert ei.value.status == 404
        with pytest.raises(ApiError):
            source_dir(paths, bad)
    with pytest.raises(ApiError):
        mgr.status("src-20261101-ffff")  # well formed but unknown


def test_path_traversal_in_filename_is_neutralised(tmp_home: Path):
    paths = Paths.from_env()
    info = UploadManager(paths).start("..\\..\\evil\\clip.mp4", 3)
    assert info.filename == "clip.mp4"
    assert (paths.home / "imports" / "clipper" / info.source_id / "source.mp4.part").exists()
