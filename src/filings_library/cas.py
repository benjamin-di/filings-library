"""Immutable SHA-256 Content-Addressed Store (CAS)."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SymlinkRejectedError(ValueError):
    """Raised when a CAS path or ancestor under the CAS root is a symlink."""


class CorruptedObjectError(ValueError):
    """Raised when stored CAS bytes do not match their expected SHA-256 digest."""


def validate_sha256(digest: str) -> str:
    """Validate and return a lowercase 64-character hex SHA-256 digest."""
    if not isinstance(digest, str) or not _SHA256_RE.match(digest):
        raise ValueError(f"Invalid SHA-256 digest: {digest!r}")
    return digest


class ContentAddressedStore:
    """Immutable SHA-256 CAS stored at `<root>/objects/sha256/<aa>/<sha256>`."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        if self._root.is_symlink():
            raise SymlinkRejectedError(f"CAS root must not be a symlink: {self._root}")
        self._objects_dir = self._root / "objects" / "sha256"
        self._assert_no_symlink_under_root(self._objects_dir)
        self._objects_dir.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def objects_dir(self) -> Path:
        return self._objects_dir

    def _assert_no_symlink_under_root(self, path: Path) -> None:
        """Reject symlinks at `path` or any component from `self._root` down to `path`."""
        if self._root.is_symlink():
            raise SymlinkRejectedError(f"Symlink rejected: {self._root}")
        try:
            rel = path.relative_to(self._root)
        except ValueError as exc:
            raise ValueError(f"Path escapes CAS root: {path}") from exc
        cur = self._root
        for part in rel.parts:
            cur = cur / part
            if cur.is_symlink():
                raise SymlinkRejectedError(f"Symlink rejected in CAS path: {cur}")

    def path_for(self, sha256: str) -> Path:
        digest = validate_sha256(sha256)
        path = self._objects_dir / digest[:2] / digest
        self._assert_no_symlink_under_root(path)
        return path

    def has(self, sha256: str) -> bool:
        path = self.path_for(sha256)
        return path.is_file() and not path.is_symlink()

    def put_bytes(self, data: bytes) -> str:
        """Store `data` immutably using atomic temp + fsync + replace. Returns SHA-256."""
        digest = hashlib.sha256(data).hexdigest()
        target = self.path_for(digest)
        shard_dir = target.parent
        self._assert_no_symlink_under_root(shard_dir)
        shard_dir.mkdir(parents=True, exist_ok=True)

        if target.exists():
            if target.is_symlink():
                raise SymlinkRejectedError(f"Symlink rejected at CAS target: {target}")
            return digest

        tmp_path: Path | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(prefix=f".{digest}.", suffix=".tmp", dir=shard_dir)
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                raise
            # Re-check target right before replace to guarantee immutability & no symlink race.
            self._assert_no_symlink_under_root(target)
            if target.exists():
                tmp_path.unlink(missing_ok=True)
                tmp_path = None
                return digest
            os.replace(tmp_path, target)
            tmp_path = None
            self._fsync_dir(shard_dir)
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        return digest

    def get_bytes(self, sha256: str) -> bytes:
        """Read bytes for `sha256`, rejecting symlinks and verifying digest integrity."""
        digest = validate_sha256(sha256)
        target = self.path_for(digest)
        if not target.exists():
            raise FileNotFoundError(f"CAS object not found: {digest}")
        if not target.is_file() or target.is_symlink():
            raise SymlinkRejectedError(f"CAS target is not a regular file: {target}")
        data = target.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest:
            raise CorruptedObjectError(
                f"CAS object {digest} failed integrity check (actual {actual})"
            )
        return data

    def total_bytes(self) -> int:
        """Return total size in bytes of all regular objects stored in CAS."""
        if not self._objects_dir.exists():
            return 0
        total = 0
        for shard in self._objects_dir.iterdir():
            if shard.is_symlink():
                raise SymlinkRejectedError(f"Symlink rejected in CAS: {shard}")
            if not shard.is_dir():
                continue
            for entry in shard.iterdir():
                if entry.is_symlink():
                    raise SymlinkRejectedError(f"Symlink rejected in CAS: {entry}")
                if entry.is_file() and _SHA256_RE.match(entry.name):
                    total += entry.stat().st_size
        return total

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)
