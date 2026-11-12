from __future__ import annotations

from pathlib import Path

import pytest

from agent_telegram.outputs import (
    MAX_ATTACHMENTS,
    copy_file_no_clobber,
    copy_tree_into_new_dir,
    publish_text,
    require_absolute,
    truncate_text,
    validate_attachments,
)


def test_require_absolute_rejects_relative_and_empty() -> None:
    with pytest.raises(ValueError, match="绝对路径"):
        require_absolute("relative/file.md", what="save_to")
    with pytest.raises(ValueError, match="不能为空"):
        require_absolute("  ", what="save_to")


def test_publish_text_never_overwrites_unless_asked(tmp_path: Path) -> None:
    target = tmp_path / "out" / "reply.md"
    first = publish_text(target, "one")
    second = publish_text(target, "two")
    assert (first.name, second.name) == ("reply.md", "reply-1.md")
    assert first.read_text(encoding="utf-8") == "one"
    assert second.read_text(encoding="utf-8") == "two"
    replaced = publish_text(target, "three", overwrite=True)
    assert replaced == target and target.read_text(encoding="utf-8") == "three"
    assert not list(target.parent.glob(".*.tmp"))


def test_copy_file_no_clobber_keeps_existing(tmp_path: Path) -> None:
    source = tmp_path / "image.png"
    source.write_bytes(b"new")
    target_dir = tmp_path / "figs"
    target_dir.mkdir()
    (target_dir / "image.png").write_bytes(b"old")
    copied = copy_file_no_clobber(source, target_dir)
    assert copied.name == "image-1.png"
    assert (target_dir / "image.png").read_bytes() == b"old"
    assert copied.read_bytes() == b"new"


def test_copy_tree_into_new_dir_uses_fresh_directory(tmp_path: Path) -> None:
    source = tmp_path / "run"
    (source / "iter-01").mkdir(parents=True)
    (source / "iter-01" / "image.png").write_bytes(b"png")
    (source / "result.json").write_text("{}", encoding="utf-8")
    (tmp_path / "out" / "run").mkdir(parents=True)
    destination, copied = copy_tree_into_new_dir(source, tmp_path / "out" / "run")
    assert destination.name == "run-1"
    assert sorted(path.relative_to(destination).as_posix() for path in copied) == ["iter-01/image.png", "result.json"]


def test_validate_attachments(tmp_path: Path) -> None:
    good = tmp_path / "a.txt"
    good.write_text("x", encoding="utf-8")
    assert validate_attachments([str(good)]) == [good]
    with pytest.raises(ValueError, match="不存在"):
        validate_attachments([str(tmp_path / "missing.pdf")])
    with pytest.raises(ValueError, match="不存在或不是普通文件"):
        validate_attachments([str(tmp_path)])
    with pytest.raises(ValueError, match="重复"):
        validate_attachments([str(good), str(good)])
    with pytest.raises(ValueError, match="最多"):
        validate_attachments([str(good)] * (MAX_ATTACHMENTS + 1))
    with pytest.raises(ValueError, match="绝对路径"):
        validate_attachments(["a.txt"])


def test_truncate_text() -> None:
    assert truncate_text("abc", 10) == ("abc", False)
    text, cut = truncate_text("x" * 50, 10)
    assert cut and text.startswith("x" * 10) and "共 50 字符" in text


def test_attachment_cap_stays_below_the_extension_message_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_telegram.outputs as outputs

    assert outputs.MAX_ATTACHMENT_BYTES == 40 * 1024 * 1024  # contract C6
    monkeypatch.setattr(outputs, "MAX_ATTACHMENT_BYTES", 10)
    big = tmp_path / "big.pdf"
    big.write_bytes(b"x" * 11)
    with pytest.raises(ValueError, match="上传上限"):
        validate_attachments([str(big)])
    with pytest.raises(ValueError, match="上传上限"):
        outputs.check_upload_size(big, what="审阅文件")


def test_review_zip_is_checked_by_member_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import zipfile

    import agent_telegram.outputs as outputs

    monkeypatch.setattr(outputs, "MAX_ATTACHMENT_BYTES", 100)
    archive = tmp_path / "paper.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        handle.writestr("main.tex", "a" * 90)
    outputs.check_upload_size(archive, what="审阅文件", zip_members=True)
    with zipfile.ZipFile(archive, "a", compression=zipfile.ZIP_DEFLATED) as handle:
        handle.writestr("figs/huge.png", "b" * 500)  # compresses below the cap, unpacks above it
    assert archive.stat().st_size < 500
    with pytest.raises(ValueError, match="figs/huge.png"):
        outputs.check_upload_size(archive, what="审阅文件", zip_members=True)


def test_output_targets_are_checked_before_work(tmp_path: Path) -> None:
    from agent_telegram.outputs import require_dir_target, require_file_target

    folder = tmp_path / "notes"
    folder.mkdir()
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("x", encoding="utf-8")
    assert require_file_target(str(tmp_path / "new" / "reply.md"), what="save_to") == tmp_path / "new" / "reply.md"
    for bad in (str(folder), str(tmp_path / "later") + "\\", str(blocker / "reply.md")):
        with pytest.raises(ValueError, match="save_to"):
            require_file_target(bad, what="save_to")
    assert require_dir_target(str(tmp_path / "a" / "b"), what="output_dir") == tmp_path / "a" / "b"
    for bad in (str(blocker), str(blocker / "sub")):
        with pytest.raises(ValueError, match="output_dir"):
            require_dir_target(bad, what="output_dir")
