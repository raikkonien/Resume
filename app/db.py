from contextlib import closing
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
from typing import Any

from .config import ADMIN_PASSWORD, DATA_DIR, DB_PATH, INITIAL_ADMIN_PASSWORD, MCP_TOKEN
from .passwords import hash_password
from .sections import BASIC, SECTIONS, Section, decode_authors

OCR_DEFAULTS = {
    "endpoint": "",
    "model": "",
    "api_key": "",
}

write_lock = threading.RLock()
_USERNAME = re.compile(r"[a-z][a-z0-9_-]{2,31}\Z", re.ASCII)
_RESERVED = {"admin", "backup", "backups", "uploads"}
_PUBLIC_USER_FIELDS = ("id", "username", "role", "session_version")


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    return db


def columns(section: Section) -> dict[str, str]:
    result = {}
    for field in section.fields:
        if field.type == "checkbox":
            result[field.key] = "INTEGER NOT NULL DEFAULT 0"
        elif field.type in ("year", "int"):
            result[field.key] = "INTEGER"
        elif field.type == "number":
            result[field.key] = "REAL"
        else:
            result[field.key] = "TEXT"
        if field.type == "file":
            result[f"{field.key}_name"] = "TEXT"
    return result


def validate_username(username: str, *, allow_admin: bool = False) -> None:
    if not isinstance(username, str) or not _USERNAME.fullmatch(username):
        raise ValueError("用户名须为 3–32 位小写英文字母、数字、下划线或短横线，且以字母开头")
    if username in _RESERVED and not (allow_admin and username == "admin"):
        raise ValueError("该用户名为系统保留名称")


def _validate_password(password: str) -> None:
    if not isinstance(password, str) or not 8 <= len(password) <= 256:
        raise ValueError("密码须为 8–256 个字符")


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {key: user[key] for key in _PUBLIC_USER_FIELDS}


def get_user(db: sqlite3.Connection, id: int) -> dict[str, Any] | None:
    row = db.execute("SELECT * FROM users WHERE id = ?", (id,)).fetchone()
    return dict(row) if row is not None else None


def get_user_by_username(db: sqlite3.Connection, username: str) -> dict[str, Any] | None:
    row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return dict(row) if row is not None else None


def list_users(db: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(
        "SELECT id, username, role, session_version FROM users ORDER BY id ASC"
    )]


def get_mcp_user(db: sqlite3.Connection, token: str) -> dict[str, Any] | None:
    if not token:
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    row = db.execute(
        "SELECT users.id, users.username, users.role, users.session_version "
        "FROM users JOIN mcp_credentials ON mcp_credentials.user_id = users.id "
        "WHERE mcp_credentials.token_hash = ? AND users.role = 'user'", (digest,),
    ).fetchone()
    return dict(row) if row is not None else None


def _require_profile(db: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise ValueError("用户编号无效")
    user = get_user(db, user_id)
    if user is None or user["role"] != "user":
        raise ValueError("该账户没有个人简历")
    return user


def _storage_path(name: str):
    # Journal names are single path components, including private deletion tombstones.
    if not re.fullmatch(r"[a-z0-9_.-]+", name) or name in (".", ".."):
        raise ValueError("用户文件目录无效")
    path = DATA_DIR.resolve() / name
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError("用户文件目录与已有文件或符号链接冲突")
    return path


def _check_username_available(db: sqlite3.Connection, username: str) -> None:
    if get_user_by_username(db, username) is not None:
        raise ValueError("用户名已存在")
    if any(path.name.casefold() == username.casefold() for path in DATA_DIR.iterdir()):
        raise ValueError("用户名与已有数据目录或文件冲突")


def _move_storage(source: str, target: str) -> None:
    old, new = _storage_path(source), _storage_path(target)
    if old.exists():
        if new.exists():
            raise ValueError("文件迁移目标已存在；为保护数据，未覆盖任何文件")
        old.rename(new)
    elif not new.exists():
        # Empty/new profiles may not yet have an on-disk directory.
        new.mkdir()


def _sync_storage_directory() -> None:
    descriptor = os.open(DATA_DIR.resolve(), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _recover_storage_operations(db: sqlite3.Connection) -> None:
    """Replay committed intents; filesystem moves never overwrite another directory.

    Each intent survives a crash before/after rename. Its DB update and removal
    commit together, so replay never increments a session version twice.
    """
    operations = db.execute("SELECT * FROM storage_operations ORDER BY id").fetchall()
    for operation in operations:
        with db:
            db.execute("BEGIN IMMEDIATE")
            # Another startup may have finished this intent while we waited.
            current = db.execute(
                "SELECT * FROM storage_operations WHERE id = ?", (operation["id"],)
            ).fetchone()
            if current is None:
                continue
            kind = current["kind"]
            if kind == "create":
                _storage_path(current["target"]).mkdir(exist_ok=True)
            elif kind in ("legacy", "rename"):
                _move_storage(current["source"], current["target"])
                if kind == "rename":
                    db.execute(
                        "UPDATE users SET username = ?, "
                        "password_hash = COALESCE(?, password_hash), "
                        "session_version = session_version + 1 WHERE id = ?",
                        (current["target"], current["password_hash"], current["user_id"]),
                    )
            elif kind == "delete":
                old, tombstone = _storage_path(current["source"]), _storage_path(current["target"])
                if old.exists():
                    if tombstone.exists():
                        raise ValueError("删除暂存目录冲突；未删除任何文件")
                    old.rename(tombstone)
                db.execute("DELETE FROM users WHERE id = ?", (current["user_id"],))
                if tombstone.exists():
                    shutil.rmtree(tombstone)
            # Flush the directory entry before retiring its durable replay intent.
            _sync_storage_directory()
            db.execute("DELETE FROM storage_operations WHERE id = ?", (current["id"],))


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _create_section(db: sqlite3.Connection, section: Section, table: str,
                    extra_columns: dict[str, str] | None = None) -> None:
    definitions = columns(section)
    definitions.update(extra_columns or {})
    fields = ", ".join(f'{_quote(key)} {kind}' for key, kind in definitions.items())
    owner = "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE"
    if section.single:
        owner += " UNIQUE"
    else:
        owner += ", sort_order INTEGER"
    db.execute(
        f'CREATE TABLE {_quote(table)} ('
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
        f'{owner}, {fields})'
    )


def _migrate_section(db: sqlite3.Connection, section: Section, original_id: int | None) -> None:
    information = db.execute(f'PRAGMA table_info({_quote(section.key)})').fetchall()
    if not information:
        _create_section(db, section, section.key)
    elif "user_id" not in {row["name"] for row in information}:
        # Rebuild legacy tables to remove basic's CHECK(id = 1), retaining IDs,
        # existing attachment paths, timestamps, custom ordering and extra fields.
        definitions = columns(section)
        extra = {}
        for row in information:
            if row["name"] in {"id", "created_at", "sort_order"} or row["name"] in definitions:
                continue
            kind = row["type"] or "TEXT"
            if row["notnull"]:
                kind += " NOT NULL"
            if row["dflt_value"] is not None:
                kind += f' DEFAULT {row["dflt_value"]}'
            extra[row["name"]] = kind
        temporary = f"__tenant_{section.key}"
        _create_section(db, section, temporary, extra)
        existing = [row["name"] for row in information]
        fields = ", ".join(_quote(key) for key in existing)
        if original_id is None and db.execute(f'SELECT 1 FROM {_quote(section.key)} LIMIT 1').fetchone():
            raise ValueError("旧数据缺少原始用户归属，迁移已停止")
        db.execute(
            f'INSERT INTO {_quote(temporary)} ({fields}, user_id) '
            f'SELECT {fields}, ? FROM {_quote(section.key)}', (original_id,),
        )
        sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (section.key,)).fetchone()
        db.execute(f'DROP TABLE {_quote(section.key)}')
        db.execute(f'ALTER TABLE {_quote(temporary)} RENAME TO {_quote(section.key)}')
        if sequence is not None:
            db.execute("UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?", (sequence[0], section.key))
    existing = {row["name"] for row in db.execute(f'PRAGMA table_info({_quote(section.key)})')}
    for key, kind in columns(section).items():
        if key not in existing:
            db.execute(f'ALTER TABLE {_quote(section.key)} ADD COLUMN {_quote(key)} {kind}')
    if not section.single:
        if "sort_order" not in existing:
            db.execute(f'ALTER TABLE {_quote(section.key)} ADD COLUMN sort_order INTEGER')
        owners = db.execute(f'SELECT DISTINCT user_id FROM {_quote(section.key)} WHERE sort_order IS NULL').fetchall()
        for owner in owners:
            user_id = owner["user_id"]
            start = db.execute(
                f'SELECT COALESCE(MAX(sort_order), -1) + 1 FROM {_quote(section.key)} WHERE user_id = ?',
                (user_id,),
            ).fetchone()[0]
            unmigrated = db.execute(
                f'SELECT id FROM {_quote(section.key)} WHERE user_id = ? AND sort_order IS NULL ORDER BY id',
                (user_id,),
            ).fetchall()
            db.executemany(
                f'UPDATE {_quote(section.key)} SET sort_order = ? WHERE id = ? AND user_id = ?',
                [(start + offset, row["id"], user_id) for offset, row in enumerate(unmigrated)],
            )
        db.execute(
            f'CREATE INDEX IF NOT EXISTS {_quote(section.key + "_owner_order")} '
            f'ON {_quote(section.key)} (user_id, sort_order, id)'
        )


def init_db() -> None:
    with write_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        generated_password = None
        with closing(connect()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE IF NOT EXISTS app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS users ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, "
                    "password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin', 'user')), "
                    "session_version INTEGER NOT NULL DEFAULT 1)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS mcp_credentials ("
                    "user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, "
                    "token_hash TEXT NOT NULL UNIQUE)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS storage_operations ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL "
                    "CHECK(kind IN ('legacy', 'create', 'rename', 'delete')), "
                    "user_id INTEGER, source TEXT, target TEXT NOT NULL, password_hash TEXT)"
                )
                bootstrapped = db.execute("SELECT value FROM app_metadata WHERE key = 'bootstrap_complete'").fetchone()
                if bootstrapped is None:
                    legacy_tables = any(
                        db.execute(f'PRAGMA table_info({_quote(section.key)})').fetchone()
                        for section in SECTIONS.values()
                    )
                    uploads = DATA_DIR / "uploads"
                    if not ADMIN_PASSWORD and (legacy_tables or uploads.exists() or uploads.is_symlink()):
                        raise RuntimeError("旧数据首次迁移须设置 ADMIN_PASSWORD，以保留原用户密码")
                    original_id = None
                    if ADMIN_PASSWORD:
                        _check_username_available(db, "zhangrui")
                        _storage_path("uploads")
                    password = INITIAL_ADMIN_PASSWORD
                    if password:
                        _validate_password(password)
                    else:
                        password = generated_password = secrets.token_urlsafe(24)
                    if ADMIN_PASSWORD:
                        original_id = db.execute(
                            "INSERT INTO users (username, password_hash, role) VALUES ('zhangrui', ?, 'user')",
                            (hash_password(ADMIN_PASSWORD),),
                        ).lastrowid
                    db.execute(
                        "INSERT INTO users (username, password_hash, role) VALUES ('admin', ?, 'admin')",
                        (hash_password(password),),
                    )
                    if MCP_TOKEN and original_id is not None:
                        db.execute(
                            "INSERT INTO mcp_credentials (user_id, token_hash) VALUES (?, ?)",
                            (original_id, hashlib.sha256(MCP_TOKEN.encode("utf-8")).hexdigest()),
                        )
                    db.execute(
                        "INSERT INTO app_metadata (key, value) VALUES ('bootstrap_complete', '1')"
                    )
                    if original_id is not None:
                        db.execute(
                            "INSERT INTO app_metadata (key, value) VALUES ('original_user_id', ?)",
                            (str(original_id),),
                        )
                        db.execute(
                            "INSERT INTO storage_operations (kind, user_id, source, target) VALUES ('legacy', ?, 'uploads', 'zhangrui')",
                            (original_id,),
                        )
                else:
                    stored = db.execute("SELECT value FROM app_metadata WHERE key = 'original_user_id'").fetchone()
                    original_id = int(stored[0]) if stored and get_user(db, int(stored[0])) else None
                for section in SECTIONS.values():
                    _migrate_section(db, section, original_id)
                db.execute(
                    "INSERT INTO basic (user_id) SELECT users.id FROM users "
                    "WHERE role = 'user' AND NOT EXISTS (SELECT 1 FROM basic WHERE basic.user_id = users.id)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS ocr_settings ("
                    "id INTEGER PRIMARY KEY CHECK (id = 1), "
                    "endpoint TEXT NOT NULL, model TEXT NOT NULL, api_key TEXT NOT NULL)"
                )
                db.execute(
                    "INSERT OR IGNORE INTO ocr_settings (id, endpoint, model, api_key) VALUES (1, ?, ?, ?)",
                    (OCR_DEFAULTS["endpoint"], OCR_DEFAULTS["model"], OCR_DEFAULTS["api_key"]),
                )
            if generated_password is not None:
                logging.warning("首次启动管理员账号 admin，初始密码（仅显示一次）：%s", generated_password)
            _recover_storage_operations(db)
            for user in list_users(db):
                if user["role"] == "user":
                    _storage_path(user["username"]).mkdir(exist_ok=True)


def create_user(db: sqlite3.Connection, username: str, password: str) -> dict[str, Any]:
    validate_username(username)
    _validate_password(password)
    password_hash = hash_password(password)
    with write_lock:
        _recover_storage_operations(db)
        with db:
            db.execute("BEGIN IMMEDIATE")
            _check_username_available(db, username)
            user_id = db.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'user')",
                (username, password_hash),
            ).lastrowid
            db.execute("INSERT INTO basic (user_id) VALUES (?)", (user_id,))
            db.execute(
                "INSERT INTO storage_operations (kind, user_id, target) VALUES ('create', ?, ?)",
                (user_id, username),
            )
        _recover_storage_operations(db)
        return _public_user(get_user(db, user_id))


def update_user(db: sqlite3.Connection, id: int, username: str,
                password: str | None = None) -> dict[str, Any]:
    validate_username(username, allow_admin=True)
    if password is not None:
        _validate_password(password)
    password_hash = hash_password(password) if password is not None else None
    with write_lock:
        _recover_storage_operations(db)
        with db:
            db.execute("BEGIN IMMEDIATE")
            user = get_user(db, id)
            if user is None:
                raise ValueError("用户不存在")
            if username != user["username"]:
                if user["role"] == "admin" or username == "admin":
                    raise ValueError("管理员用户名不可修改或使用")
                _check_username_available(db, username)
                _storage_path(user["username"])
                db.execute(
                    "INSERT INTO storage_operations (kind, user_id, source, target, password_hash) "
                    "VALUES ('rename', ?, ?, ?, ?)", (id, user["username"], username, password_hash),
                )
            elif password_hash is not None:
                db.execute(
                    "UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE id = ?",
                    (password_hash, id),
                )
        _recover_storage_operations(db)
        return _public_user(get_user(db, id))


def delete_user(db: sqlite3.Connection, id: int) -> None:
    with write_lock:
        _recover_storage_operations(db)
        with db:
            db.execute("BEGIN IMMEDIATE")
            user = get_user(db, id)
            if user is None:
                raise ValueError("用户不存在")
            if user["role"] == "admin":
                raise ValueError("管理员账户不可删除")
            _storage_path(user["username"])
            tombstone = f".deleted-user-{id}-{secrets.token_hex(8)}"
            db.execute(
                "INSERT INTO storage_operations (kind, user_id, source, target) VALUES ('delete', ?, ?, ?)",
                (id, user["username"], tombstone),
            )
        _recover_storage_operations(db)


def get_ocr_settings(db: sqlite3.Connection) -> dict[str, str]:
    return dict(db.execute("SELECT endpoint, model, api_key FROM ocr_settings WHERE id = 1").fetchone())


def save_ocr_settings(db: sqlite3.Connection, endpoint: str, model: str, api_key: str | None) -> None:
    with write_lock, db:
        db.execute(
            "UPDATE ocr_settings SET endpoint = ?, model = ?, api_key = COALESCE(?, api_key) WHERE id = 1",
            (endpoint, model, api_key),
        )


def get_basic(db: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    _require_profile(db, user_id)
    row = db.execute("SELECT * FROM basic WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row)


def _decode_row(section: Section, row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for field in section.fields:
        if field.type == "authors":
            result[field.key] = decode_authors(result.get(field.key))
    return result


def _encode_values(section: Section, values: dict[str, Any]) -> dict[str, Any]:
    encoded = dict(values)
    for field in section.fields:
        if field.type == "authors" and field.key in encoded:
            encoded[field.key] = json.dumps(decode_authors(encoded[field.key]), ensure_ascii=False)
    return encoded


def list_rows(db: sqlite3.Connection, section: Section, user_id: int) -> list[dict[str, Any]]:
    _require_profile(db, user_id)
    order = "id ASC" if section.single else "sort_order ASC, id ASC"
    return [_decode_row(section, row) for row in db.execute(
        f'SELECT * FROM "{section.key}" WHERE user_id = ? ORDER BY {order}', (user_id,)
    )]


def get_row(db: sqlite3.Connection, section: Section, id: int, user_id: int) -> dict[str, Any] | None:
    _require_profile(db, user_id)
    row = db.execute(f'SELECT * FROM "{section.key}" WHERE id = ? AND user_id = ?', (id, user_id)).fetchone()
    return _decode_row(section, row) if row is not None else None


def insert_row(db: sqlite3.Connection, section: Section, values: dict[str, Any], user_id: int) -> int:
    if section.single:
        raise ValueError("个人基本信息仅有一条记录")
    if not values.keys() <= columns(section).keys():
        raise ValueError("未知字段")
    values = _encode_values(section, values)
    with write_lock, db:
        _require_profile(db, user_id)
        next_order = db.execute(
            f'SELECT COALESCE(MAX(sort_order), -1) + 1 FROM "{section.key}" WHERE user_id = ?', (user_id,)
        ).fetchone()[0]
        keys = [*values.keys(), "sort_order", "user_id"]
        assignments = ", ".join(f'"{key}"' for key in keys)
        placeholders = ", ".join("?" for _ in keys)
        cursor = db.execute(
            f'INSERT INTO "{section.key}" ({assignments}) VALUES ({placeholders})',
            (*values.values(), next_order, user_id),
        )
    return cursor.lastrowid


def update_row(db: sqlite3.Connection, section: Section, id: int,
               values: dict[str, Any], user_id: int) -> None:
    if not values.keys() <= columns(section).keys():
        raise ValueError("未知字段")
    values = _encode_values(section, values)
    with write_lock, db:
        _require_profile(db, user_id)
        if not values:
            return
        assignments = ", ".join(f'"{key}" = ?' for key in values)
        db.execute(
            f'UPDATE "{section.key}" SET {assignments} WHERE id = ? AND user_id = ?',
            (*values.values(), id, user_id),
        )


def delete_row(db: sqlite3.Connection, section: Section, id: int, user_id: int) -> None:
    if section.single:
        raise ValueError("个人基本信息不可删除")
    with write_lock, db:
        _require_profile(db, user_id)
        db.execute(f'DELETE FROM "{section.key}" WHERE id = ? AND user_id = ?', (id, user_id))


def reorder_rows(db: sqlite3.Connection, section: Section, ids: list[int], user_id: int) -> None:
    if section.single:
        raise ValueError("个人基本信息不可排序")
    if not isinstance(ids, list):
        raise ValueError("排序参数无效")
    seen: set[int] = set()
    for value in ids:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("排序参数包含无效记录编号")
        if value in seen:
            raise ValueError("排序参数包含重复记录")
        seen.add(value)
    with write_lock, db:
        _require_profile(db, user_id)
        existing_ids = {row["id"] for row in db.execute(
            f'SELECT id FROM "{section.key}" WHERE user_id = ?', (user_id,)
        )}
        if seen != existing_ids:
            raise ValueError("排序参数与现有记录不一致")
        db.executemany(
            f'UPDATE "{section.key}" SET sort_order = ? WHERE id = ? AND user_id = ?',
            [(order, id, user_id) for order, id in enumerate(ids)],
        )
