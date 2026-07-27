import io
import tarfile

import pytest

from app.config import Settings
from app.utils.backup import BackupError, _extract_tar_safely, backup_files


def add_bytes(tar: tarfile.TarFile, name: str, payload: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(payload)
    tar.addfile(member, io.BytesIO(payload))


def test_extract_tar_safely_extracts_regular_files(tmp_path):
    archive = tmp_path / "safe.tar"
    destination = tmp_path / "destination"
    destination.mkdir()

    with tarfile.open(archive, "w") as tar:
        add_bytes(tar, "backup/metadata.json", b"{}")

    with tarfile.open(archive) as tar:
        _extract_tar_safely(tar, destination)

    assert (destination / "backup" / "metadata.json").read_bytes() == b"{}"


def test_extract_tar_safely_rejects_path_traversal(tmp_path):
    archive = tmp_path / "traversal.tar"
    destination = tmp_path / "destination"
    destination.mkdir()

    with tarfile.open(archive, "w") as tar:
        add_bytes(tar, "../outside.txt", b"not allowed")

    with tarfile.open(archive) as tar:
        with pytest.raises(BackupError, match="outside dest"):
            _extract_tar_safely(tar, destination)

    assert not (tmp_path / "outside.txt").exists()


def test_extract_tar_safely_rejects_links(tmp_path):
    archive = tmp_path / "link.tar"
    destination = tmp_path / "destination"
    destination.mkdir()

    with tarfile.open(archive, "w") as tar:
        link = tarfile.TarInfo("backup/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "metadata.json"
        tar.addfile(link)

    with tarfile.open(archive) as tar:
        with pytest.raises(BackupError, match="tar link"):
            _extract_tar_safely(tar, destination)


def test_backup_files_uses_typed_settings_paths(tmp_path):
    storage = tmp_path / "storage"
    staging = tmp_path / "staging"
    storage.mkdir()
    staging.mkdir()
    (storage / "document.txt").write_text("stored", encoding="utf-8")
    (staging / "pending.txt").write_text("pending", encoding="utf-8")
    settings = Settings(storage_folder=str(storage), staging_folder=str(staging))

    result = backup_files(tmp_path / "backup", settings)

    assert (result / "storage" / "document.txt").read_text(encoding="utf-8") == "stored"
    assert (result / "staging" / "pending.txt").read_text(encoding="utf-8") == "pending"
