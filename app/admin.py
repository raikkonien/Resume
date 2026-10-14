import asyncio
from contextlib import AsyncExitStack, closing
import json
import sqlite3
import threading

import anyio.from_thread
from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.responses import JSONResponse, StreamingResponse

from .auth import require_session_user
from .config import MAX_UPLOAD_MB
from .db import (
    ArchiveDeletionPending, ArchiveRecordNotFound, _discard_new_row, _recover_archive_deletions,
    archive_row, connect, delete_archived_rows, get_basic, get_ocr_settings, get_row,
    get_table_layout, get_upload_preferences, insert_row, list_rows, parse_archive_records,
    reorder_rows, restore_rows, save_table_layout, save_upload_preference, update_row, write_lock,
)
from .files import delete_entry_dir, delete_upload, save_upload, upload_path, validate_upload
from .management import active_session_user
from .ocr import RecognitionCancelled, recognize
from .sections import BASIC, SECTIONS, parse_form, record_label
from .templating import templates

router = APIRouter(prefix="/api")


def entry_section(key):
    section = SECTIONS.get(key)
    if section is None or section.single:
        raise HTTPException(404)
    return section


def require_row(db, section, id, user_id):
    row = get_row(db, section, id, user_id)
    if row is None:
        raise HTTPException(404)
    return row




def entry_position(rows, id):
    for index, row in enumerate(rows):
        if row["id"] == id:
            return index + 1
    raise HTTPException(404)




def entry_fragment(request, section, row, position, status_code=200):
    with closing(connect()) as db:
        preferences = get_upload_preferences(db, section, require_session_user(request)["id"])
    return templates.TemplateResponse(request, "_entry.html", {
        "section": section, "row": row, "position": position,
        "max_upload_mb": MAX_UPLOAD_MB,
        "upload_preferences": preferences,
    }, status_code=status_code)


def table_fragment(request, section, entries, status_code=200):
    with closing(connect()) as db:
        basic = get_basic(db, require_session_user(request)["id"])
    return templates.TemplateResponse(request, "_entry_table.html", {
        "section": section, "entries": entries, "basic": basic, "overview": False,
    }, status_code=status_code)


def collect_uploads(section, form):
    uploads = [(f, form.get(f.key)) for f in section.fields if f.type == "file"]
    return [(f, u) for f, u in uploads if isinstance(u, UploadFile) and u.filename]


def save_form(request: Request, section, id, form):
    """Validate a complete entry submission, then persist it.

    `id` is None for a brand-new record (nothing exists in the database yet)
    and an existing row id for an edit. Returns the section's rows in their
    persistent display order.
    """
    values = parse_form(section, form)
    uploads = [
        (field, upload, validate_upload(field, upload))
        for field, upload in collect_uploads(section, form)
    ]
    file_fields = {f.key for f in section.fields if f.type == "file"}
    upload_keys = {field.key for field, _, _ in uploads}
    removals = (set(form.getlist("remove_file")) & file_fields) - upload_keys if id is not None else set()
    creating = id is None
    with write_lock, closing(connect()) as db:
        user = active_session_user(request, db)
        user_id = user["id"]
        if section.single:
            id = get_basic(db, user_id)["id"]
        if creating:
            id = insert_row(db, section, values, user_id)
        else:
            require_row(db, section, id, user_id)
        try:
            for field, upload, extension in uploads:
                path, name = save_upload(user["username"], section.key, id, field, upload, extension)
                values[field.key] = path
                values[f"{field.key}_name"] = name
            if removals:
                current = get_row(db, section, id, user_id)
                for key in removals:
                    delete_upload(user["username"], current[key])
                    values[key] = None
                    values[f"{key}_name"] = None
        except ValueError:
            if creating:
                _discard_new_row(db, section, id, user_id)
                delete_entry_dir(user["username"], section.key, id)
            raise
        except OSError as error:
            if creating:
                _discard_new_row(db, section, id, user_id)
                delete_entry_dir(user["username"], section.key, id)
            raise ValueError("文件保存失败，请重试") from error
        update_row(db, section, id, values, user_id,
                   activity_action="create" if creating else "update",
                   force_activity=creating or bool(uploads))
        return list_rows(db, section, user_id)


def reorder_entry_rows(request: Request, section, ids):
    with write_lock, closing(connect()) as db:
        user_id = active_session_user(request, db)["id"]
        reorder_rows(db, section, ids, user_id)
        return list_rows(db, section, user_id)




def ndjson_line(payload):
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


async def stream_recognition(form_cm, schema, upload, settings, personal_name, personal_pinyin, source_field):
    """Stream page progress while keeping the upload alive until its worker exits."""
    sender, receiver = anyio.create_memory_object_stream(1)
    cancelled = threading.Event()

    def emit(kind, payload):
        if cancelled.is_set():
            raise RecognitionCancelled("已取消识别")
        try:
            anyio.from_thread.run(sender.send, (kind, payload))
        except (anyio.BrokenResourceError, anyio.ClosedResourceError) as error:
            raise RecognitionCancelled("已取消识别") from error

    def on_page_start(page):
        emit("progress", page)

    def worker():
        try:
            try:
                result = recognize(
                    schema, upload, settings, personal_name, personal_pinyin, source_field,
                    on_page_start=on_page_start, should_stop=cancelled.is_set,
                )
            except ValueError as error:
                emit("error", str(error))
            else:
                emit("result", result)
        except RecognitionCancelled:
            return
        finally:
            anyio.from_thread.run_sync(sender.close)

    task = asyncio.create_task(run_in_threadpool(worker))
    try:
        async for kind, payload in receiver:
            if kind == "progress":
                yield ndjson_line({"type": "progress", "page": payload})
                continue
            if kind == "error":
                yield ndjson_line({"type": "error", "error": payload})
            else:
                yield ndjson_line({"type": "result", **payload})
            break
    finally:
        cancelled.set()
        # Closing the bounded channel releases a producer blocked on a slow client.
        # Shield cleanup from Starlette's disconnect scope; a current provider call
        # may finish, but the upload must not close while the worker still uses it.
        with anyio.CancelScope(shield=True):
            receiver.close()
            try:
                await task
            finally:
                await form_cm.__aexit__(None, None, None)


@router.post("/basic/ocr")
async def recognize_basic(request: Request):
    return await recognize_upload(request, BASIC)


@router.post("/entries/{section}/ocr")
async def recognize_entry(request: Request, section: str):
    require_session_user(request)
    return await recognize_upload(request, entry_section(section))


async def recognize_upload(request: Request, schema):
    require_session_user(request)
    form_cm = request.form(max_files=1, max_fields=1)
    form = await form_cm.__aenter__()
    upload = form.get("file")
    source_field = form.get("field", "")
    file_fields = {field.key for field in schema.fields if field.type == "file"}
    if not source_field and len(file_fields) == 1:
        source_field = next(iter(file_fields))
    try:
        if not isinstance(source_field, str) or source_field not in file_fields:
            raise ValueError("附件字段无效")
        with write_lock, closing(connect()) as db:
            user_id = active_session_user(request, db)["id"]
            if not get_upload_preferences(db, schema, user_id)[source_field]:
                raise ValueError("该附件已关闭 AI 识别，仅保存上传文件")
            if not isinstance(upload, UploadFile) or not upload.filename:
                raise ValueError("请选择 PDF、JPG 或 PNG 文件")
            if upload.filename.lower().endswith(".zip"):
                raise ValueError("ZIP 附件仅保存上传文件，不支持 AI 识别")
            settings = get_ocr_settings(db, user_id)
            basic = get_basic(db, user_id)
    except ValueError as error:
        await form_cm.__aexit__(None, None, None)
        return JSONResponse({"error": str(error)}, status_code=400)
    except Exception:
        await form_cm.__aexit__(None, None, None)
        raise
    return StreamingResponse(
        stream_recognition(
            form_cm, schema, upload, settings, basic.get("name"), basic.get("pinyin"), source_field,
        ),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no"},
    )


def open_saved_paper(request, id):
    """Check the current session and opt-out before opening the owner-scoped PDF."""
    with write_lock, closing(connect()) as db:
        user = active_session_user(request, db)
        user_id = user["id"]
        row = require_row(db, SECTIONS["papers"], id, user_id)
        if not get_upload_preferences(db, SECTIONS["papers"], user_id)["article_pdf"]:
            raise ValueError("该附件已关闭 AI 识别，仅保存上传文件")
        if not row["article_pdf"]:
            raise ValueError("尚未上传文章 PDF，请先上传并保存")
        path = upload_path(user["username"], row["article_pdf"])
        if path.suffix.lower() != ".pdf":
            raise ValueError("重新识别仅支持已保存的 PDF；图片可重新选择上传进行识别")
        settings = get_ocr_settings(db, user_id)
        basic = get_basic(db, user_id)
        try:
            file = path.open("rb")
        except OSError:
            raise ValueError("已保存的 PDF 无法读取，请重新上传") from None
    return UploadFile(file=file, filename=path.name), settings, basic


@router.post("/entries/papers/{id}/ocr")
async def recognize_saved_paper(request: Request, id: int):
    require_session_user(request)
    try:
        upload, settings, basic = await run_in_threadpool(open_saved_paper, request, id)
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    resources = AsyncExitStack()
    resources.push_async_callback(upload.close)
    return StreamingResponse(
        stream_recognition(
            resources, SECTIONS["papers"], upload, settings,
            basic.get("name"), basic.get("pinyin"), "article_pdf",
        ),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
    )


@router.get("/basic/edit")
def edit_basic(request: Request):
    user = require_session_user(request)
    with closing(connect()) as db:
        basic = get_basic(db, user["id"])
        preferences = get_upload_preferences(db, BASIC, user["id"])
    return templates.TemplateResponse(request, "_basic_form.html", {
        "basic": basic, "basic_section": BASIC, "max_upload_mb": MAX_UPLOAD_MB,
        "upload_preferences": preferences,
    })


@router.put("/basic")
async def save_basic(request: Request):
    require_session_user(request)
    async with request.form() as form:
        try:
            entries = await run_in_threadpool(save_form, request, BASIC, 0, form)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
    return table_fragment(request, BASIC, entries)


@router.get("/entries/{section}/new")
def new_entry(request: Request, section: str):
    user_id = require_session_user(request)["id"]
    schema = entry_section(section)
    with closing(connect()) as db:
        rows = list_rows(db, schema, user_id)
    return entry_fragment(request, schema, {"id": None}, len(rows) + 1)


@router.get("/entries/{section}/{id}/edit")
def edit_entry(request: Request, section: str, id: int):
    user_id = require_session_user(request)["id"]
    schema = entry_section(section)
    with closing(connect()) as db:
        row = require_row(db, schema, id, user_id)
        rows = list_rows(db, schema, user_id)
    return entry_fragment(request, schema, row, entry_position(rows, id))


@router.post("/entries/{section}")
async def create_entry(request: Request, section: str):
    require_session_user(request)
    schema = entry_section(section)
    async with request.form() as form:
        try:
            entries = await run_in_threadpool(save_form, request, schema, None, form)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
    return table_fragment(request, schema, entries, status_code=201)


@router.put("/entries/{section}/{id}")
async def save_entry(request: Request, section: str, id: int):
    schema = entry_section(section)
    require_session_user(request)
    async with request.form() as form:
        try:
            entries = await run_in_threadpool(save_form, request, schema, id, form)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
    return table_fragment(request, schema, entries)


@router.post("/entries/{section}/{id}/archive")
def archive_entry(request: Request, section: str, id: int):
    require_session_user(request)
    schema = entry_section(section)
    with write_lock, closing(connect()) as db:
        user_id = active_session_user(request, db)["id"]
        require_row(db, schema, id, user_id)
        archive_row(db, schema, id, user_id)
        entries = list_rows(db, schema, user_id)
    return table_fragment(request, schema, entries)



@router.post("/entries/{section}/reorder")
async def reorder_entries(request: Request, section: str):
    schema = entry_section(section)
    require_session_user(request)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "请求格式错误"}, status_code=400)
    ids = body.get("ids") if isinstance(body, dict) else None
    if not isinstance(ids, list):
        return JSONResponse({"error": "请求格式错误"}, status_code=400)
    try:
        entries = await run_in_threadpool(reorder_entry_rows, request, schema, ids)
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    return table_fragment(request, schema, entries)


@router.get("/entries/{section}")
def read_entries(request: Request, section: str):
    user_id = require_session_user(request)["id"]
    schema = entry_section(section)
    with closing(connect()) as db:
        entries = list_rows(db, schema, user_id)
    return table_fragment(request, schema, entries)


@router.get("/archive")
def read_archive(request: Request):
    user_id = require_session_user(request)["id"]
    with closing(connect()) as db:
        db.execute("BEGIN")
        records = [
            {"section": section.key, "id": row["id"], "title": record_label(section, row),
             "archived_at": row["archived_at"]}
            for section in SECTIONS.values() if not section.single
            for row in list_rows(db, section, user_id, archived=True)
        ]
    records.sort(key=lambda row: (row["archived_at"], row["id"]), reverse=True)
    return records


def mutate_archive(request: Request, records, *, deleting: bool) -> None:
    with write_lock, closing(connect()) as db:
        user_id = active_session_user(request, db)["id"]
        try:
            _recover_archive_deletions(db, user_id=user_id)
        except (OSError, ValueError, sqlite3.Error) as error:
            raise ArchiveDeletionPending(
                "永久删除尚未完成，部分附件可能已移除；删除任务已保留，将在下次操作或重启时继续。请重新加载查看状态。"
            ) from error
        active_session_user(request, db)
        if deleting:
            delete_archived_rows(db, records, user_id)
        else:
            restore_rows(db, records, user_id)


async def archive_batch_response(request: Request, *, deleting: bool):
    require_session_user(request)
    try:
        records = parse_archive_records(await request.json())
    except (ValueError, UnicodeDecodeError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    try:
        await run_in_threadpool(mutate_archive, request, records, deleting=deleting)
    except ArchiveRecordNotFound as error:
        return JSONResponse({"error": str(error)}, status_code=404)
    except ArchiveDeletionPending as error:
        return JSONResponse({"error": str(error)}, status_code=503)
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    except (OSError, sqlite3.Error):
        return JSONResponse({"error": "归档操作失败，请重试或重新加载"}, status_code=503)
    return {"deleted" if deleting else "restored": len(records)}


@router.post("/archive/restore")
async def restore_archive(request: Request):
    return await archive_batch_response(request, deleting=False)


@router.post("/archive/delete")
async def delete_archive(request: Request):
    return await archive_batch_response(request, deleting=True)


def layout_section(key):
    section = SECTIONS.get(key)
    if section is None:
        raise HTTPException(404)
    return section


@router.get("/layouts/{section}")
def read_layout(request: Request, section: str):
    user_id = require_session_user(request)["id"]
    schema = layout_section(section)
    with closing(connect()) as db:
        return get_table_layout(db, schema, user_id)


@router.put("/layouts/{section}")
async def write_layout(request: Request, section: str):
    require_session_user(request)
    schema = layout_section(section)
    try:
        body = await request.json()
        with write_lock, closing(connect()) as db:
            user_id = active_session_user(request, db)["id"]
            return save_table_layout(db, schema, user_id, body)
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)


@router.get("/upload-preferences/{section}/{field}")
def read_upload_preference(request: Request, section: str, field: str):
    require_session_user(request)
    schema = layout_section(section)
    with write_lock, closing(connect()) as db:
        user_id = active_session_user(request, db)["id"]
        preferences = get_upload_preferences(db, schema, user_id)
        if field not in preferences:
            return JSONResponse({"error": "附件字段无效"}, status_code=400)
        return {"enabled": preferences[field]}


@router.put("/upload-preferences/{section}/{field}")
async def write_upload_preference(request: Request, section: str, field: str):
    require_session_user(request)
    schema = layout_section(section)
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"enabled"}:
            raise ValueError("请求必须仅包含 enabled")
        with write_lock, closing(connect()) as db:
            user_id = active_session_user(request, db)["id"]
            return save_upload_preference(db, schema, user_id, field, body["enabled"])
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
