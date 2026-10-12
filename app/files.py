from pathlib import Path
import shutil
import tempfile

from PIL import Image
from starlette.datastructures import UploadFile

from . import db
from .config import MAX_UPLOAD_MB
from .sections import Field, SECTIONS


IMAGE_EXTENSIONS = {"JPEG": (".jpg", ".jpeg"), "PNG": (".png",)}


def validate_upload(field: Field, upload: UploadFile) -> str:
    """Return the allowed content extension, correcting mislabeled JPEG/PNG images."""
    extension = Path(upload.filename or "").suffix.lower()
    accepted = field.accept.split(",")
    if extension not in accepted:
        raise ValueError("仅支持 " + field.accept)
    upload.file.seek(0, 2)
    size = upload.file.tell()
    upload.file.seek(0)
    if size > MAX_UPLOAD_MB * 1024 * 1024:
        raise ValueError(f"文件超过 {MAX_UPLOAD_MB}MB")
    if extension == ".pdf":
        prefix = upload.file.read(4)
        upload.file.seek(0)
        if not prefix.startswith(b"%PDF"):
            raise ValueError(f"{field.label}：文件内容不是 PDF，请重新导出后上传")
        return extension
    try:
        with Image.open(upload.file, formats=tuple(IMAGE_EXTENSIONS)) as image:
            extensions = IMAGE_EXTENSIONS[image.format]
            allowed = [suffix for suffix in extensions if suffix in accepted]
            if not allowed:
                raise ValueError("图片实际格式不受支持")
            if extension not in allowed:
                extension = allowed[0]
            image.verify()
        upload.file.seek(0)
        with Image.open(upload.file, formats=tuple(IMAGE_EXTENSIONS)) as image:
            image.load()
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as error:
        raise ValueError(
            f"{field.label}：图片内容无法识别、已损坏或格式不受支持，请重新导出为 {field.accept} 后上传"
        ) from error
    finally:
        upload.file.seek(0)
    return extension


def user_directory(username: str) -> Path:
    """Resolve a profile root without following a user-controlled symlink."""
    db.validate_username(username)
    directory = db.DATA_DIR.resolve() / username
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ValueError("用户文件目录无效")
    return directory


def upload_path(username: str, rel_path: str) -> Path:
    """Resolve a stored relative path; reject traversal and every symlink component."""
    directory = user_directory(username)
    if not isinstance(rel_path, str) or not rel_path or "\\" in rel_path or "\0" in rel_path:
        raise ValueError("文件路径无效")
    parts = rel_path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("文件路径无效")
    path = directory
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("文件路径不可包含符号链接")
    if not path.resolve().is_relative_to(directory):
        raise ValueError("文件路径无效")
    return path


def entry_directory(username: str, section: str, entry_id: int) -> Path:
    """Validate every component of one record's attachment directory."""
    if section not in SECTIONS or isinstance(entry_id, bool) or not isinstance(entry_id, int) or entry_id < 1:
        raise ValueError("记录文件目录无效")
    directory = upload_path(username, f"{section}/{entry_id}")
    if (directory.parent.exists() and not directory.parent.is_dir()) or (directory.exists() and not directory.is_dir()):
        raise ValueError("记录文件目录无效")
    return directory


def save_upload(username: str, section: str, entry_id: int, field: Field,
                upload: UploadFile, extension: str) -> tuple[str, str]:
    """Persist an already-validated upload inside exactly one profile directory."""
    with db.write_lock:
        directory = entry_directory(username, section, entry_id)
        if not any(item.key == field.key and item.type == "file" for item in SECTIONS[section].fields):
            raise ValueError("文件字段无效")
        if extension not in {".pdf", ".jpg", ".jpeg", ".png"} or extension not in field.accept.split(","):
            raise ValueError("文件扩展名无效")
        destination = upload_path(username, f"{section}/{entry_id}/{field.key}{extension}")
        directory.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            total = 0
            with tempfile.NamedTemporaryFile(mode="wb", dir=directory, prefix=f".{field.key}.",
                                             suffix=".part", delete=False) as output:
                temporary = Path(output.name)
                while chunk := upload.file.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_UPLOAD_MB * 1024 * 1024:
                        raise ValueError(f"文件超过 {MAX_UPLOAD_MB}MB")
                    output.write(chunk)
            # Replace first, so a failed rename never destroys the current file.
            temporary.replace(destination)
            for old in directory.glob(f"{field.key}.*"):
                if old != destination:
                    old.unlink(missing_ok=True)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        name = Path((upload.filename or destination.name).replace("\\", "/")).name
        if Path(name).suffix.lower() != extension:
            name = str(Path(name).with_suffix(extension))
        return destination.relative_to(user_directory(username)).as_posix(), name


def delete_upload(username: str, rel_path: str | None) -> None:
    with db.write_lock:
        if rel_path:
            upload_path(username, rel_path).unlink(missing_ok=True)
        else:
            user_directory(username)


def delete_entry_dir(username: str, section: str, id: int) -> None:
    with db.write_lock:
        directory = entry_directory(username, section, id)
        if directory.exists():
            shutil.rmtree(directory)
