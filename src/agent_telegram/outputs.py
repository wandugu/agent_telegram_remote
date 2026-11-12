"""Filesystem helpers: absolute-path checks, no-clobber publication, hashing."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from agent_telegram.i18n import tr
from agent_telegram.fsutil import replace_atomic

MAX_ATTACHMENTS = 10
# The userscript downloads each attachment through one Chrome extension message
# (64 MiB cap, base64 adds a third), so a single file must stay below 40 MB.
MAX_ATTACHMENT_BYTES = 40 * 1024 * 1024
_MIB = 1024 * 1024


def require_absolute(value: str | os.PathLike[str], *, what: str) -> Path:
    text = os.fspath(value).strip()
    if not text:
        raise ValueError(tr('{v0} cannot be empty.', v0=what))
    if "\x00" in text:
        raise ValueError(tr('{v0} contains a NUL character.', v0=what))
    path = Path(text)
    if not path.is_absolute():
        raise ValueError(tr('{v0} must be an absolute path: {v1}', v0=what, v1=text))
    return Path(os.path.abspath(path))


def _nearest_existing_ancestor_is_dir(path: Path) -> bool:
    for parent in path.parents:
        if parent.exists():
            return parent.is_dir()
    return False


def require_file_target(value: str | os.PathLike[str], *, what: str) -> Path:
    """An absolute output file path that can be created (checked before any GPT work)."""

    text = os.fspath(value).strip()
    path = require_absolute(text, what=what)
    if text.endswith(("/", "\\")) or path.is_dir():
        raise ValueError(tr('{v0} must be a file path (for example …\\reply.md), not a directory: {v1}', v0=what, v1=text))
    if not _nearest_existing_ancestor_is_dir(path):
        raise ValueError(tr('The parent of {v0} is not an available directory: {v1}', v0=what, v1=path))
    return path


def require_dir_target(value: str | os.PathLike[str], *, what: str) -> Path:
    """An absolute output directory that exists or can be created."""

    path = require_absolute(value, what=what)
    if path.exists() and not path.is_dir():
        raise ValueError(tr('{v0} must be a directory, but a file with that name already exists: {v1}', v0=what, v1=path))
    if not path.exists() and not _nearest_existing_ancestor_is_dir(path):
        raise ValueError(tr('The parent of {v0} is not an available directory: {v1}', v0=what, v1=path))
    return path


def _too_large(what: str, size: int) -> ValueError:
    return ValueError(
        tr('{v0} is {v1:.1f} MB, exceeding the {v2} MB per-file upload limit (Chrome extension single-message limit); compress or split it, or upload only the part you need.', v0=what, v1=size / _MIB, v2=MAX_ATTACHMENT_BYTES // _MIB)
    )


def check_upload_size(path: Path, *, what: str | None = None, zip_members: bool = False) -> None:
    """Reject a file ChatGPT would receive through the extension channel if it is too large.

    With ``zip_members`` (document review unpacks ZIPs before upload) the members are checked instead.
    """

    what = what if what is not None else tr('Attachment')
    if zip_members and path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path) as archive:
                members = [member for member in archive.infolist() if not member.is_dir()]
        except (OSError, zipfile.BadZipFile):
            members = None  # the review step reports unreadable archives itself
        if members is not None:
            for member in members:
                if member.file_size > MAX_ATTACHMENT_BYTES:
                    raise _too_large(tr('{v2} in {v0} {v1}', v0=what, v1=path, v2=member.filename), member.file_size)
            return
    size = path.stat().st_size
    if size > MAX_ATTACHMENT_BYTES:
        raise _too_large(f"{what} {path}", size)


def validate_attachments(files: list[str]) -> list[Path]:
    """Accept only explicit, existing regular files; explicit paths authorize upload."""

    if len(files) > MAX_ATTACHMENTS:
        raise ValueError(tr('At most {v0} files may be uploaded at once; received {v1}.', v0=MAX_ATTACHMENTS, v1=len(files)))
    paths: list[Path] = []
    seen: set[str] = set()
    for item in files:
        path = require_absolute(item, what=tr('Attachment path'))
        if not path.is_file():
            raise ValueError(tr('Attachment does not exist or is not a regular file: {v0}', v0=path))
        size = path.stat().st_size
        if size > MAX_ATTACHMENT_BYTES:
            raise _too_large(tr('Attachment {v0}', v0=path), size)
        key = os.path.normcase(str(path))
        if key in seen:
            raise ValueError(tr('Duplicate attachment: {v0}', v0=path))
        seen.add(key)
        paths.append(path)
    return paths


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "name": path.name,
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    return tr('{v0}\n\n…[Truncated: {v1} characters total; see the saved file for the full content]', v0=text[:max_chars], v1=len(text)), True


def _temp_sibling(path: Path) -> Path:
    return path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")


def _replace_with_retry(source: Path, target: Path, attempts: int = 40) -> None:
    replace_atomic(source, target)


def _write_temp(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = _temp_sibling(path)
    with temp.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return temp


def atomic_write_bytes(path: Path, data: bytes) -> Path:
    temp = _write_temp(path, data)
    try:
        _replace_with_retry(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return path


def atomic_write_text(path: Path, text: str) -> Path:
    return atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, value: Any) -> Path:
    return atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def read_json(path: Path, attempts: int = 5) -> Any:
    for attempt in range(attempts):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (PermissionError, json.JSONDecodeError):
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)
    raise AssertionError("unreachable")


def _numbered_files(path: Path) -> Iterator[Path]:
    yield path
    for index in range(1, 10_000):
        yield path.with_name(f"{path.stem}-{index}{path.suffix}")


def _numbered_dirs(path: Path) -> Iterator[Path]:
    yield path
    for index in range(1, 10_000):
        yield path.with_name(f"{path.name}-{index}")


def _rename_no_clobber(source: Path, target: Path) -> bool:
    if os.name == "nt":
        try:
            os.rename(source, target)  # Windows rename never replaces an existing file.
        except FileExistsError:
            return False
        return True
    try:
        os.link(source, target)
    except FileExistsError:
        return False
    source.unlink()
    return True


def _publish_temp(temp: Path, path: Path) -> Path:
    try:
        for candidate in _numbered_files(path):
            if _rename_no_clobber(temp, candidate):
                return candidate
        raise FileExistsError(tr('No filename is available near {v0}.', v0=path))
    finally:
        temp.unlink(missing_ok=True)


def publish_bytes(path: Path, data: bytes, *, overwrite: bool = False) -> Path:
    """Write data to path; an existing file is kept unless overwrite is true.

    Without overwrite, a taken name becomes ``name-1.ext``, ``name-2.ext`` ...
    Returns the path actually written.
    """

    if overwrite:
        return atomic_write_bytes(path, data)
    return _publish_temp(_write_temp(path, data), path)


def publish_text(path: Path, text: str, *, overwrite: bool = False) -> Path:
    return publish_bytes(path, text.encode("utf-8"), overwrite=overwrite)


def copy_file_no_clobber(source: Path, target_dir: Path, name: str | None = None) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / (name or source.name)
    temp = _temp_sibling(target)
    shutil.copy2(source, temp)
    return _publish_temp(temp, target)


def unique_directory(path: Path) -> Path:
    """Create and return path, or path-1, path-2 ... when the name is taken."""

    path.parent.mkdir(parents=True, exist_ok=True)
    for candidate in _numbered_dirs(path):
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError(tr('No directory name is available near {v0}.', v0=path))


def copy_tree_into_new_dir(source_dir: Path, target_dir: Path) -> tuple[Path, list[Path]]:
    """Copy every regular file below source_dir into a freshly created directory."""

    destination = unique_directory(target_dir)
    copied: list[Path] = []
    for item in sorted(source_dir.rglob("*")):
        if item.is_symlink() or not item.is_file():
            continue
        target = destination / item.relative_to(source_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        copied.append(target)
    return destination, copied
