from contextlib import closing
from datetime import date, datetime, timezone
import hashlib
import json
import logging
import math
import os
import re
import secrets
import shutil
import sqlite3
import threading
from typing import Any

from .config import ADMIN_PASSWORD, DATA_DIR, DB_PATH, INITIAL_ADMIN_PASSWORD, MCP_TOKEN
from .passwords import hash_password
from .sections import BASIC, SECTIONS, Section, decode_authors, record_label

OCR_DEFAULTS = {
    "endpoint": "",
    "model": "",
    "api_key": "",
}

write_lock = threading.RLock()
_USERNAME = re.compile(r"[a-z][a-z0-9_-]{2,31}\Z", re.ASCII)
_RESERVED = {"backup", "backups", "uploads"}
_PUBLIC_USER_FIELDS = ("id", "username", "session_version")


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


def validate_username(username: str) -> None:
    if not isinstance(username, str) or not _USERNAME.fullmatch(username):
        raise ValueError("用户名须为 3–32 位小写英文字母、数字、下划线或短横线，且以字母开头")
    if username in _RESERVED:
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
        "SELECT id, username, session_version FROM users ORDER BY id ASC"
    )]


def get_mcp_user(db: sqlite3.Connection, token: str) -> dict[str, Any] | None:
    if not token:
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    row = db.execute(
        "SELECT users.id, users.username, users.session_version "
        "FROM users JOIN mcp_credentials ON mcp_credentials.user_id = users.id "
        "WHERE mcp_credentials.token_hash = ?", (digest,),
    ).fetchone()
    return dict(row) if row is not None else None


def _require_user(db: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise ValueError("用户编号无效")
    user = get_user(db, user_id)
    if user is None:
        raise ValueError("用户不存在")
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


def _recover_storage_operations(db: sqlite3.Connection, *, archive_user_id: int | None = None) -> None:
    """Replay committed intents; filesystem moves never overwrite another directory.

    Each intent survives a crash before/after rename. Its DB update and removal
    commit together, so replay never increments a session version twice.
    """
    _recover_archive_deletions(db, user_id=archive_user_id)
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
    if not section.single:
        definitions["archived_at"] = "TEXT"
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
        if "archived_at" not in existing:
            db.execute(f'ALTER TABLE {_quote(section.key)} ADD COLUMN archived_at TEXT')
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


def _migrate_ocr_settings(db: sqlite3.Connection, recipient: int | None) -> None:
    existing = {row["name"] for row in db.execute("PRAGMA table_info(ocr_settings)")}
    if existing and "user_id" not in existing:
        old = db.execute("SELECT endpoint, model, api_key FROM ocr_settings WHERE id = 1").fetchone()
        if old is not None and any(old) and recipient is None:
            raise RuntimeError("无法确定旧模型配置归属")
        db.execute(
            "CREATE TABLE __workspace_ocr_settings ("
            "user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, "
            "endpoint TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', api_key TEXT NOT NULL DEFAULT '')"
        )
        if old is not None and recipient is not None:
            db.execute(
                "INSERT INTO __workspace_ocr_settings (user_id, endpoint, model, api_key) VALUES (?, ?, ?, ?)",
                (recipient, *old),
            )
        db.execute("DROP TABLE ocr_settings")
        db.execute("ALTER TABLE __workspace_ocr_settings RENAME TO ocr_settings")
    elif not existing:
        db.execute(
            "CREATE TABLE ocr_settings ("
            "user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, "
            "endpoint TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', api_key TEXT NOT NULL DEFAULT '')"
        )
    db.execute("INSERT OR IGNORE INTO ocr_settings (user_id) SELECT id FROM users")


def init_db() -> None:
    # Never rebuild users: doing so would cascade-delete owner-scoped data.
    if sqlite3.sqlite_version_info < (3, 35, 0):
        raise RuntimeError("简历工作台升级要求 SQLite >= 3.35")
    with write_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        generated_password = None
        with closing(connect()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
                recipient = None
                if "role" in user_columns:
                    old_users = db.execute("SELECT id, username, role FROM users").fetchall()
                    personal = [row["id"] for row in old_users if row["role"] == "user"]
                    recipient = personal[0] if len(personal) == 1 else next(
                        (row["id"] for row in old_users if row["role"] == "admin" and row["username"] == "admin"), None
                    )
                    db.execute("ALTER TABLE users DROP COLUMN role")
                db.execute("CREATE TABLE IF NOT EXISTS app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS users ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, "
                    "password_hash TEXT NOT NULL, session_version INTEGER NOT NULL DEFAULT 1)"
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
                db.execute(
                    "CREATE TABLE IF NOT EXISTS archive_deletions ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
                    "records_json TEXT NOT NULL)"
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
                            "INSERT INTO users (username, password_hash) VALUES ('zhangrui', ?)",
                            (hash_password(ADMIN_PASSWORD),),
                        ).lastrowid
                        recipient = original_id
                    db.execute(
                        "INSERT INTO users (username, password_hash) VALUES ('admin', ?)",
                        (hash_password(password),),
                    )
                    if MCP_TOKEN and original_id is not None:
                        db.execute(
                            "INSERT INTO mcp_credentials (user_id, token_hash) VALUES (?, ?)",
                            (original_id, hashlib.sha256(MCP_TOKEN.encode("utf-8")).hexdigest()),
                        )
                    db.execute("INSERT INTO app_metadata (key, value) VALUES ('bootstrap_complete', '1')")
                    if original_id is not None:
                        db.execute(
                            "INSERT INTO app_metadata (key, value) VALUES ('original_user_id', ?)", (str(original_id),),
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
                    "WHERE NOT EXISTS (SELECT 1 FROM basic WHERE basic.user_id = users.id)"
                )
                _migrate_ocr_settings(db, recipient)
                db.execute(
                    "CREATE TABLE IF NOT EXISTS record_events ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
                    "section_key TEXT NOT NULL, entry_id INTEGER, "
                    "action TEXT NOT NULL CHECK(action IN ('create','update','archive','restore','reorder')), "
                    "title TEXT NOT NULL, occurred_at TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS record_events_recent ON record_events (user_id, occurred_at DESC, id DESC)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS table_layouts ("
                    "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
                    "section_key TEXT NOT NULL, layout_json TEXT NOT NULL, PRIMARY KEY(user_id, section_key))"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS upload_preferences ("
                    "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
                    "section_key TEXT NOT NULL, field_key TEXT NOT NULL, "
                    "enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)), "
                    "PRIMARY KEY(user_id, section_key, field_key))"
                )
                db.execute(
                    "INSERT INTO app_metadata (key, value) VALUES ('workspace_schema_version', '1') "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                )
            if generated_password is not None:
                logging.warning("首次启动账号 admin，初始密码（仅显示一次）：%s", generated_password)
            _recover_storage_operations(db)
            for user in list_users(db):
                _storage_path(user["username"]).mkdir(exist_ok=True)


def create_user(db: sqlite3.Connection, username: str, password: str) -> dict[str, Any]:
    validate_username(username)
    _validate_password(password)
    password_hash = hash_password(password)
    with write_lock:
        # A not-yet-created account has no archive deletion to replay.
        _recover_storage_operations(db, archive_user_id=0)
        with db:
            db.execute("BEGIN IMMEDIATE")
            _check_username_available(db, username)
            user_id = db.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (username, password_hash),
            ).lastrowid
            db.execute("INSERT INTO basic (user_id) VALUES (?)", (user_id,))
            db.execute("INSERT INTO ocr_settings (user_id) VALUES (?)", (user_id,))
            db.execute(
                "INSERT INTO storage_operations (kind, user_id, target) VALUES ('create', ?, ?)",
                (user_id, username),
            )
        _recover_storage_operations(db, archive_user_id=user_id)
        return _public_user(get_user(db, user_id))


def update_user(db: sqlite3.Connection, id: int, username: str,
                password: str | None = None) -> dict[str, Any]:
    validate_username(username)
    if password is not None:
        _validate_password(password)
    password_hash = hash_password(password) if password is not None else None
    with write_lock:
        _recover_storage_operations(db, archive_user_id=id)
        with db:
            db.execute("BEGIN IMMEDIATE")
            user = get_user(db, id)
            if user is None:
                raise ValueError("用户不存在")
            if username != user["username"]:
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
        _recover_storage_operations(db, archive_user_id=id)
        return _public_user(get_user(db, id))


def delete_user(db: sqlite3.Connection, id: int) -> None:
    with write_lock:
        _recover_storage_operations(db, archive_user_id=id)
        with db:
            db.execute("BEGIN IMMEDIATE")
            user = get_user(db, id)
            if user is None:
                raise ValueError("用户不存在")
            _storage_path(user["username"])
            tombstone = f".deleted-user-{id}-{secrets.token_hex(8)}"
            db.execute(
                "INSERT INTO storage_operations (kind, user_id, source, target) VALUES ('delete', ?, ?, ?)",
                (id, user["username"], tombstone),
            )
        _recover_storage_operations(db, archive_user_id=id)


def get_ocr_settings(db: sqlite3.Connection, user_id: int) -> dict[str, str]:
    _require_user(db, user_id)
    row = db.execute("SELECT endpoint, model, api_key FROM ocr_settings WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        raise ValueError("用户模型配置不存在")
    return dict(row)


def save_ocr_settings(db: sqlite3.Connection, user_id: int, endpoint: str, model: str,
                      api_key: str | None) -> None:
    with write_lock, db:
        _require_user(db, user_id)
        db.execute(
            "UPDATE ocr_settings SET endpoint = ?, model = ?, api_key = COALESCE(?, api_key) WHERE user_id = ?",
            (endpoint, model, api_key, user_id),
        )



def _upload_fields(section: Section) -> set[str]:
    schema = SECTIONS.get(section.key)
    if schema is None:
        raise ValueError("未知栏目")
    return {field.key for field in schema.fields if field.type == "file"}


def get_upload_preferences(db: sqlite3.Connection, section: Section, user_id: int) -> dict[str, bool]:
    _require_user(db, user_id)
    preferences = dict.fromkeys(_upload_fields(section), True)
    for row in db.execute(
        "SELECT field_key, enabled FROM upload_preferences WHERE user_id = ? AND section_key = ?",
        (user_id, section.key),
    ):
        if row["field_key"] in preferences:
            preferences[row["field_key"]] = bool(row["enabled"])
    return preferences


def save_upload_preference(db: sqlite3.Connection, section: Section, user_id: int,
                           field_key: str, enabled: bool) -> dict[str, bool]:
    if not isinstance(field_key, str) or field_key not in _upload_fields(section):
        raise ValueError("附件字段无效")
    if type(enabled) is not bool:
        raise ValueError("enabled 必须为布尔值")
    with write_lock, db:
        db.execute("BEGIN IMMEDIATE")
        _require_user(db, user_id)
        db.execute(
            "INSERT INTO upload_preferences (user_id, section_key, field_key, enabled) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, section_key, field_key) DO UPDATE SET enabled = excluded.enabled",
            (user_id, section.key, field_key, int(enabled)),
        )
    return {"enabled": enabled}

def get_basic(db: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    _require_user(db, user_id)
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


def list_rows(db: sqlite3.Connection, section: Section, user_id: int, *,
              archived: bool = False) -> list[dict[str, Any]]:
    _require_user(db, user_id)
    if section.single and archived:
        return []
    scope = "" if section.single else (" AND archived_at IS NOT NULL" if archived else " AND archived_at IS NULL")
    order = "id ASC" if section.single else "sort_order ASC, id ASC"
    return [_decode_row(section, row) for row in db.execute(
        f'SELECT * FROM "{section.key}" WHERE user_id = ?{scope} ORDER BY {order}', (user_id,)
    )]


def get_row(db: sqlite3.Connection, section: Section, id: int, user_id: int, *,
            include_archived: bool = False) -> dict[str, Any] | None:
    _require_user(db, user_id)
    scope = "" if section.single or include_archived else " AND archived_at IS NULL"
    row = db.execute(
        f'SELECT * FROM "{section.key}" WHERE id = ? AND user_id = ?{scope}', (id, user_id)
    ).fetchone()
    return _decode_row(section, row) if row is not None else None


def insert_row(db: sqlite3.Connection, section: Section, values: dict[str, Any], user_id: int) -> int:
    if section.single:
        raise ValueError("个人基本信息仅有一条记录")
    if not values.keys() <= columns(section).keys():
        raise ValueError("未知字段")
    values = _encode_values(section, values)
    with write_lock, db:
        db.execute("BEGIN IMMEDIATE")
        _require_user(db, user_id)
        next_order = db.execute(
            f'SELECT COALESCE(MAX(sort_order), -1) + 1 FROM "{section.key}" WHERE user_id = ? AND archived_at IS NULL', (user_id,)
        ).fetchone()[0]
        keys = [*values.keys(), "sort_order", "user_id"]
        assignments = ", ".join(f'"{key}"' for key in keys)
        placeholders = ", ".join("?" for _ in keys)
        cursor = db.execute(
            f'INSERT INTO "{section.key}" ({assignments}) VALUES ({placeholders})',
            (*values.values(), next_order, user_id),
        )
    return cursor.lastrowid


def _record_event(db: sqlite3.Connection, section: Section, user_id: int, entry_id: int | None,
                  action: str, row: dict[str, Any] | None = None) -> None:
    db.execute(
        "INSERT INTO record_events (user_id, section_key, entry_id, action, title, occurred_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, section.key, entry_id, action, section.label if row is None else record_label(section, row),
         datetime.now(timezone.utc).isoformat()),
    )


def update_row(db: sqlite3.Connection, section: Section, id: int,
               values: dict[str, Any], user_id: int, *, activity_action: str = "update",
               force_activity: bool = False) -> None:
    if activity_action not in ("create", "update") or not isinstance(force_activity, bool):
        raise ValueError("修改事件参数无效")
    if not values.keys() <= columns(section).keys():
        raise ValueError("未知字段")
    values = _encode_values(section, values)
    with write_lock, db:
        db.execute("BEGIN IMMEDIATE")
        _require_user(db, user_id)
        before = get_row(db, section, id, user_id)
        if before is None:
            return
        if values:
            assignments = ", ".join(f'"{key}" = ?' for key in values)
            db.execute(
                f'UPDATE "{section.key}" SET {assignments} WHERE id = ? AND user_id = ?',
                (*values.values(), id, user_id),
            )
        after = get_row(db, section, id, user_id)
        if before != after or force_activity or activity_action == "create":
            _record_event(db, section, user_id, id, activity_action, after)


def _discard_new_row(db: sqlite3.Connection, section: Section, id: int, user_id: int) -> None:
    """Compensate for failed new-record uploads; not a public deletion operation."""
    if section.single:
        raise ValueError("个人基本信息不可删除")
    with write_lock, db:
        db.execute("BEGIN IMMEDIATE")
        _require_user(db, user_id)
        db.execute(f'DELETE FROM "{section.key}" WHERE id = ? AND user_id = ?', (id, user_id))


def archive_row(db: sqlite3.Connection, section: Section, id: int, user_id: int) -> None:
    if section.single:
        raise ValueError("个人基本信息不可归档")
    with write_lock, db:
        db.execute("BEGIN IMMEDIATE")
        _require_user(db, user_id)
        row = get_row(db, section, id, user_id)
        if row is None:
            raise ValueError("记录不存在或已归档")
        db.execute(
            f'UPDATE "{section.key}" SET archived_at = ? WHERE id = ? AND user_id = ?',
            (datetime.now(timezone.utc).isoformat(), id, user_id),
        )
        _record_event(db, section, user_id, id, "archive", row)


class ArchiveRecordNotFound(ValueError):
    """A batch member is missing or belongs to another account."""


class ArchiveDeletionPending(RuntimeError):
    """A durable deletion will be completed by storage recovery."""


def parse_archive_records(body: Any) -> list[tuple[Section, int]]:
    if not isinstance(body, dict) or set(body) != {"records"}:
        raise ValueError("请求必须仅包含 records")
    records = body["records"]
    if not isinstance(records, list) or not records:
        raise ValueError("请选择至少一条归档记录")
    result = []
    seen = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"section", "id"}:
            raise ValueError("归档记录参数无效")
        key, id = record["section"], record["id"]
        section = SECTIONS.get(key) if isinstance(key, str) else None
        if section is None or section.single or type(id) is not int or id < 1:
            raise ValueError("归档栏目或记录编号无效")
        if (key, id) in seen:
            raise ValueError("归档记录不可重复")
        seen.add((key, id))
        result.append((section, id))
    return result


def _archived_batch(db: sqlite3.Connection, records: list[tuple[Section, int]],
                    user_id: int) -> list[dict[str, Any]]:
    _require_user(db, user_id)
    rows = []
    for section, id in records:
        try:
            row = get_row(db, section, id, user_id, include_archived=True)
        except OverflowError:
            row = None  # A positive ID outside SQLite's integer range cannot exist.
        if row is None:
            raise ArchiveRecordNotFound("归档记录不存在，请重新加载")
        if row["archived_at"] is None:
            raise ValueError("仅可操作已归档记录")
        rows.append(row)
    return rows


def _restore_archived_row(db: sqlite3.Connection, section: Section, id: int,
                          user_id: int, row: dict[str, Any]) -> None:
    next_order = db.execute(
        f'SELECT COALESCE(MAX(sort_order), -1) + 1 FROM "{section.key}" WHERE user_id = ? AND archived_at IS NULL',
        (user_id,),
    ).fetchone()[0]
    db.execute(
        f'UPDATE "{section.key}" SET archived_at = NULL, sort_order = ? WHERE id = ? AND user_id = ?',
        (next_order, id, user_id),
    )
    _record_event(db, section, user_id, id, "restore", row)


def restore_rows(db: sqlite3.Connection, records: list[tuple[Section, int]], user_id: int) -> None:
    with write_lock, db:
        db.execute("BEGIN IMMEDIATE")
        rows = _archived_batch(db, records, user_id)
        for (section, id), row in zip(records, rows):
            _restore_archived_row(db, section, id, user_id, row)


def restore_row(db: sqlite3.Connection, section: Section, id: int, user_id: int) -> None:
    records = parse_archive_records({"records": [{"section": section.key, "id": id}]})
    with write_lock:
        _recover_storage_operations(db, archive_user_id=user_id)
        restore_rows(db, records, user_id)


def _recover_archive_deletions(db: sqlite3.Connection, *, user_id: int | None = None) -> None:
    from .files import delete_entry_dir, entry_directory

    scope = "" if user_id is None else " WHERE user_id = ?"
    parameters = () if user_id is None else (user_id,)
    operations = db.execute(f"SELECT id FROM archive_deletions{scope} ORDER BY id", parameters).fetchall()
    for operation in operations:
        with db:
            db.execute("BEGIN IMMEDIATE")
            intent = db.execute("SELECT * FROM archive_deletions WHERE id = ?", (operation["id"],)).fetchone()
            if intent is None:
                continue
            user = _require_user(db, intent["user_id"])
            records = parse_archive_records(json.loads(intent["records_json"]))
            _archived_batch(db, records, user["id"])
            # Preflight the entire set on every replay, before removing any directory.
            for section, id in records:
                entry_directory(user["username"], section.key, id)
            for section, id in records:
                directory = entry_directory(user["username"], section.key, id)
                delete_entry_dir(user["username"], section.key, id)
                # Persist each removed entry before retiring the durable intent.
                if directory.parent.exists():
                    descriptor = os.open(directory.parent, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            for section, id in records:
                db.execute(f'DELETE FROM "{section.key}" WHERE id = ? AND user_id = ?', (id, user["id"]))
                db.execute(
                    "DELETE FROM record_events WHERE user_id = ? AND section_key = ? AND entry_id = ?",
                    (user["id"], section.key, id),
                )
            db.execute("DELETE FROM archive_deletions WHERE id = ?", (intent["id"],))


def delete_archived_rows(db: sqlite3.Connection, records: list[tuple[Section, int]], user_id: int) -> None:
    from .files import entry_directory

    with write_lock:
        with db:
            db.execute("BEGIN IMMEDIATE")
            user = _require_user(db, user_id)
            _archived_batch(db, records, user_id)
            try:
                for section, id in records:
                    entry_directory(user["username"], section.key, id)
            except ValueError as error:
                raise OSError("归档附件目录无效；未开始删除") from error
            db.execute(
                "INSERT INTO archive_deletions (user_id, records_json) VALUES (?, ?)",
                (user_id, json.dumps({"records": [{"section": section.key, "id": id} for section, id in records]})),
            )
        try:
            _recover_archive_deletions(db, user_id=user_id)
        except (OSError, ValueError, sqlite3.Error) as error:
            raise ArchiveDeletionPending(
                "永久删除尚未完成，部分附件可能已移除；删除任务已保留，将在下次操作或重启时继续。请重新加载查看状态。"
            ) from error


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
        db.execute("BEGIN IMMEDIATE")
        _require_user(db, user_id)
        existing_ids = [row["id"] for row in db.execute(
            f'SELECT id FROM "{section.key}" WHERE user_id = ? AND archived_at IS NULL ORDER BY sort_order, id', (user_id,)
        )]
        if seen != set(existing_ids):
            raise ValueError("排序参数与现有记录不一致")
        if ids == existing_ids:
            return
        db.executemany(
            f'UPDATE "{section.key}" SET sort_order = ? WHERE id = ? AND user_id = ?',
            [(order, id, user_id) for order, id in enumerate(ids)],
        )
        _record_event(db, section, user_id, None, "reorder")


def _default_layout(section: Section) -> dict[str, Any]:
    return {
        "version": 1, "column_order": [field.key for field in section.fields],
        "hidden_columns": [], "sort": None, "filters": {},
    }


def _range_value(field, value: Any) -> Any:
    if value is None:
        return None
    if field.type in ("date", "month"):
        pattern = r"[0-9]{4}-[0-9]{2}-[0-9]{2}" if field.type == "date" else r"[0-9]{4}-[0-9]{2}"
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ValueError("日期范围须使用 ISO 日期或月份")
        try:
            date.fromisoformat(value if field.type == "date" else value + "-01")
        except ValueError:
            raise ValueError("日期范围无效") from None
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("数值范围无效")
    if field.type in ("year", "int") and not isinstance(value, int):
        raise ValueError("范围须为整数")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("数值范围无效")
    if field.type == "year" and not 1900 <= value <= 2100:
        raise ValueError("年份范围无效")
    return value


def _normalize_filter(field, value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        raise ValueError("筛选参数无效")
    if field.type in ("text", "textarea", "authors", "rank"):
        if value.keys() != {"op", "value"} or value["op"] != "contains" or not isinstance(value["value"], str):
            raise ValueError("文本筛选参数无效")
        text = value["value"].strip()
        return {"op": "contains", "value": text} if text else None
    if field.type in ("select", "checkbox"):
        if value.keys() != {"op", "value"} or value["op"] != "eq":
            raise ValueError("选项筛选参数无效")
        selected = value["value"]
        if field.type == "checkbox":
            if not isinstance(selected, bool):
                raise ValueError("复选筛选须为布尔值")
        elif not isinstance(selected, str) or selected not in field.choices:
            raise ValueError("筛选选项无效")
        return {"op": "eq", "value": selected}
    if field.type == "file":
        if value.keys() != {"op", "value"} or value["op"] != "presence" or value["value"] not in ("present", "missing"):
            raise ValueError("附件筛选参数无效")
        return {"op": "presence", "value": value["value"]}
    if field.type in ("date", "month", "year", "int", "number"):
        if value.keys() != {"op", "min", "max"} or value["op"] != "range":
            raise ValueError("范围筛选参数无效")
        minimum, maximum = _range_value(field, value["min"]), _range_value(field, value["max"])
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("范围下限不能大于上限")
        return {"op": "range", "min": minimum, "max": maximum} if minimum is not None or maximum is not None else None
    raise ValueError("字段筛选类型无效")


def _normalize_layout(section: Section, layout: Any, *, loading: bool = False) -> dict[str, Any]:
    if section.key not in SECTIONS:
        raise ValueError("未知栏目")
    if not isinstance(layout, dict) or layout.keys() != {"version", "column_order", "hidden_columns", "sort", "filters"}:
        raise ValueError("布局参数无效")
    if type(layout["version"]) is not int or layout["version"] != 1:
        raise ValueError("布局版本无效")
    fields = {field.key: field for field in section.fields}
    order, hidden = layout["column_order"], layout["hidden_columns"]
    for items in (order, hidden):
        if not isinstance(items, list) or any(not isinstance(key, str) for key in items) or len(set(items)) != len(items):
            raise ValueError("列顺序或显隐参数无效")
    if loading:
        order = [key for key in order if key in fields]
        order += [key for key in fields if key not in order]
        hidden = [key for key in hidden if key in fields and key in layout["column_order"]]
    elif set(order) != set(fields) or not set(hidden) <= set(fields):
        raise ValueError("列字段无效")
    if len(hidden) == len(fields):
        raise ValueError("至少保留一个可见业务列")
    sort = layout["sort"]
    if loading and isinstance(sort, dict) and sort.get("field") not in fields:
        sort = None
    if sort is not None:
        if not isinstance(sort, dict) or sort.keys() != {"field", "direction"}:
            raise ValueError("排序参数无效")
        if not isinstance(sort["field"], str) or sort["field"] not in fields or sort["direction"] not in ("asc", "desc"):
            raise ValueError("排序字段或方向无效")
        sort = dict(sort)
    filters = layout["filters"]
    if not isinstance(filters, dict):
        raise ValueError("筛选参数无效")
    normalized_filters = {}
    for key, value in filters.items():
        if key not in fields:
            if loading:
                continue
            raise ValueError("未知筛选字段")
        try:
            normalized = _normalize_filter(fields[key], value)
        except ValueError:
            if loading:
                continue
            raise
        if normalized is not None:
            normalized_filters[key] = normalized
    return {"version": 1, "column_order": list(order), "hidden_columns": list(hidden),
            "sort": sort, "filters": normalized_filters}


def get_table_layout(db: sqlite3.Connection, section: Section, user_id: int) -> dict[str, Any]:
    _require_user(db, user_id)
    if section.key not in SECTIONS:
        raise ValueError("未知栏目")
    row = db.execute(
        "SELECT layout_json FROM table_layouts WHERE user_id = ? AND section_key = ?", (user_id, section.key)
    ).fetchone()
    if row is not None:
        try:
            return _normalize_layout(section, json.loads(row["layout_json"]), loading=True)
        except (ValueError, TypeError):
            pass
    return _default_layout(section)


def save_table_layout(db: sqlite3.Connection, section: Section, user_id: int,
                      layout: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_layout(section, layout)
    with write_lock, db:
        db.execute("BEGIN IMMEDIATE")
        _require_user(db, user_id)
        db.execute(
            "INSERT INTO table_layouts (user_id, section_key, layout_json) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, section_key) DO UPDATE SET layout_json = excluded.layout_json",
            (user_id, section.key, json.dumps(normalized, ensure_ascii=False, allow_nan=False)),
        )
    return normalized
