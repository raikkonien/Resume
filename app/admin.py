import asyncio
from contextlib import AsyncExitStack, closing
import hashlib
import hmac
import json
import threading
from urllib.parse import urlsplit

import anyio.from_thread
from fastapi import APIRouter, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.responses import JSONResponse, RedirectResponse, StreamingResponse

from .auth import current_user, require_admin, require_profile
from .config import MAX_UPLOAD_MB, SECRET_KEY
from .db import (
    connect, delete_row, get_basic, get_ocr_settings, get_row, get_user, insert_row, list_rows,
    reorder_rows, save_ocr_settings, update_row, write_lock,
)
from .files import delete_entry_dir, delete_upload, save_upload, upload_path, validate_upload
from .management import render_management
from .ocr import RecognitionCancelled, check_model, recognize
from .sections import BASIC, ENTRY_SECTIONS, SECTIONS, parse_form
from .templating import templates

router = APIRouter(prefix="/admin")
ocr_test_signer = URLSafeTimedSerializer(SECRET_KEY, salt="ocr-settings-test")


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


def profile_user(db, user_id):
    user = get_user(db, user_id)
    if user is None or user["role"] != "user":
        raise HTTPException(401)
    return user


def entry_position(rows, id):
    for index, row in enumerate(rows):
        if row["id"] == id:
            return index + 1
    raise HTTPException(404)


def render_admin(request, *, error=None, submitted=None, status_code=200):
    user = require_profile(request)
    with closing(connect()) as db:
        basic = get_basic(db, user["id"])
        rows = {s.key: list_rows(db, s, user["id"]) for s in ENTRY_SECTIONS}
    if submitted:
        for field in BASIC.fields:
            if field.type != "file" and isinstance(submitted.get(field.key), str):
                basic[field.key] = submitted[field.key]
    return templates.TemplateResponse(request, "admin.html", {
        "basic": basic, "basic_section": BASIC, "sections": tuple(SECTIONS.values()),
        "entry_sections": ENTRY_SECTIONS, "rows": rows, "error": error,
        "saved": request.query_params.get("saved") == "1",
        "max_upload_mb": MAX_UPLOAD_MB,
    }, status_code=status_code)


def entry_fragment(request, section, row, position, status_code=200):
    return templates.TemplateResponse(request, "_entry.html", {
        "section": section, "row": row, "position": position,
        "max_upload_mb": MAX_UPLOAD_MB,
    }, status_code=status_code)


def table_fragment(request, section, entries, status_code=200):
    basic = None
    if section.key == "papers":
        with closing(connect()) as db:
            basic = get_basic(db, require_profile(request)["id"])
    return templates.TemplateResponse(request, "_entry_table.html", {
        "section": section, "entries": entries, "basic": basic,
    }, status_code=status_code)


def collect_uploads(section, form):
    uploads = [(f, form.get(f.key)) for f in section.fields if f.type == "file"]
    return [(f, u) for f, u in uploads if isinstance(u, UploadFile) and u.filename]


def save_form(user_id, section, id, form):
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
        user = profile_user(db, user_id)
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
                delete_row(db, section, id, user_id)
                delete_entry_dir(user["username"], section.key, id)
            raise
        except OSError as error:
            if creating:
                delete_row(db, section, id, user_id)
                delete_entry_dir(user["username"], section.key, id)
            raise ValueError("文件保存失败，请重试") from error
        update_row(db, section, id, values, user_id)
        return list_rows(db, section, user_id)


def reorder_entry_rows(user_id, section, ids):
    with write_lock, closing(connect()) as db:
        profile_user(db, user_id)
        reorder_rows(db, section, ids, user_id)
        return list_rows(db, section, user_id)


@router.get("")
def admin_page(request: Request):
    if current_user(request)["role"] == "admin":
        return render_management(request)
    return render_admin(request)


def public_ocr_settings(settings):
    return {
        "endpoint": settings["endpoint"], "model": settings["model"],
        "has_api_key": bool(settings["api_key"]),
    }


@router.get("/ocr/settings")
def read_ocr_settings(request: Request):
    require_admin(request)
    with closing(connect()) as db:
        return public_ocr_settings(get_ocr_settings(db))


def candidate_ocr_settings(body, stored):
    if not isinstance(body, dict):
        raise ValueError("设置格式错误")
    endpoint, model, key = body.get("endpoint"), body.get("model"), body.get("api_key")
    if not isinstance(endpoint, str) or not isinstance(model, str):
        raise ValueError("请填写接口地址和模型名称")
    endpoint, model = endpoint.strip(), model.strip()
    if not endpoint or not model or len(endpoint) > 2048 or len(model) > 256:
        raise ValueError("请填写有效的接口地址和模型名称")
    url = urlsplit(endpoint)
    if (url.scheme not in ("http", "https") or not url.hostname or url.fragment
            or url.username is not None or url.password is not None
            or any(char.isspace() for char in endpoint)):
        raise ValueError("接口地址须为完整的 HTTP(S) 地址，不含用户名、密码或片段")
    url.port
    if key is None:
        key = stored["api_key"]
    elif (not isinstance(key, str) or len(key) > 4096 or not key.isascii()
          or any(ord(char) < 32 or ord(char) == 127 for char in key)):
        raise ValueError("API Key 格式错误")
    return {"endpoint": endpoint, "model": model, "api_key": key.strip()}


def settings_fingerprint(settings):
    payload = json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(SECRET_KEY.encode(), payload, hashlib.sha256).hexdigest()


@router.post("/ocr/settings/test")
async def test_ocr_settings(request: Request):
    require_admin(request)
    try:
        body = await request.json()
        with closing(connect()) as db:
            candidate = candidate_ocr_settings(body, get_ocr_settings(db))
        await run_in_threadpool(check_model, candidate)
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    # The proof contains only a keyed fingerprint, never the API key.
    return {"test_token": ocr_test_signer.dumps(settings_fingerprint(candidate))}


@router.put("/ocr/settings")
async def update_ocr_settings(request: Request):
    require_admin(request)
    try:
        body = await request.json()
        with write_lock, closing(connect()) as db:
            candidate = candidate_ocr_settings(body, get_ocr_settings(db))
            token = body.get("test_token")
            if not isinstance(token, str):
                raise ValueError("请先测试当前模型配置，测试通过后才能保存")
            try:
                tested = ocr_test_signer.loads(token, max_age=600)
            except BadSignature:
                raise ValueError("测试凭证无效或已过期，请重新测试当前配置") from None
            if not isinstance(tested, str) or not hmac.compare_digest(tested, settings_fingerprint(candidate)):
                raise ValueError("配置已变更，请重新测试后保存")
            save_ocr_settings(db, candidate["endpoint"], candidate["model"], candidate["api_key"])
            return public_ocr_settings(get_ocr_settings(db))
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)


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


@router.post("/{section}/ocr")
async def recognize_entry(request: Request, section: str):
    user = require_profile(request)
    schema = entry_section(section)
    form_cm = request.form(max_files=1, max_fields=1)
    form = await form_cm.__aenter__()
    upload = form.get("file")
    source_field = form.get("field", "")
    error = None
    if not isinstance(upload, UploadFile) or not upload.filename:
        error = "请选择 PDF、JPG 或 PNG 文件"
    elif not isinstance(source_field, str) or (source_field and not any(
        field.key == source_field and field.type == "file" for field in schema.fields
    )):
        error = "附件字段无效"
    elif section == "projects" and source_field != "contract":
        error = "该附件不支持自动识别，请手动填写"
    if error is not None:
        await form_cm.__aexit__(None, None, None)
        return JSONResponse({"error": error}, status_code=400)
    try:
        with closing(connect()) as db:
            settings = get_ocr_settings(db)
            basic = get_basic(db, user["id"])
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


def open_saved_paper(user_id, id):
    """Open an owner-scoped PDF under the storage lock; never modify the record."""
    with write_lock, closing(connect()) as db:
        user = profile_user(db, user_id)
        row = require_row(db, SECTIONS["papers"], id, user_id)
        if not row["article_pdf"]:
            raise ValueError("尚未上传文章 PDF，请先上传并保存")
        path = upload_path(user["username"], row["article_pdf"])
        if path.suffix.lower() != ".pdf":
            raise ValueError("重新识别仅支持已保存的 PDF；图片可重新选择上传进行识别")
        settings = get_ocr_settings(db)
        basic = get_basic(db, user_id)
        try:
            file = path.open("rb")
        except OSError:
            raise ValueError("已保存的 PDF 无法读取，请重新上传") from None
    return UploadFile(file=file, filename=path.name), settings, basic


@router.post("/papers/{id}/ocr")
async def recognize_saved_paper(request: Request, id: int):
    user = require_profile(request)
    try:
        upload, settings, basic = await run_in_threadpool(open_saved_paper, user["id"], id)
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


@router.post("/basic")
async def save_basic(request: Request):
    user = require_profile(request)
    async with request.form() as form:
        try:
            await run_in_threadpool(save_form, user["id"], BASIC, 0, form)
        except ValueError as error:
            return await run_in_threadpool(render_admin, request, error=str(error), submitted=form, status_code=400)
    return RedirectResponse("/admin?saved=1#basic", status_code=303)


@router.post("/basic/photo/delete")
def delete_photo(request: Request):
    user_id = require_profile(request)["id"]
    with write_lock, closing(connect()) as db:
        user = profile_user(db, user_id)
        row = get_basic(db, user_id)
        delete_upload(user["username"], row["photo"])
        update_row(db, BASIC, row["id"], {"photo": None, "photo_name": None}, user_id)
    return RedirectResponse("/admin#basic", status_code=303)


@router.get("/{section}/new")
def new_entry(request: Request, section: str):
    user_id = require_profile(request)["id"]
    schema = entry_section(section)
    with closing(connect()) as db:
        rows = list_rows(db, schema, user_id)
    return entry_fragment(request, schema, {"id": None}, len(rows) + 1)


@router.get("/{section}/{id}/edit")
def edit_entry(request: Request, section: str, id: int):
    user_id = require_profile(request)["id"]
    schema = entry_section(section)
    with closing(connect()) as db:
        row = require_row(db, schema, id, user_id)
        rows = list_rows(db, schema, user_id)
    return entry_fragment(request, schema, row, entry_position(rows, id))


@router.post("/{section}")
async def create_entry(request: Request, section: str):
    user_id = require_profile(request)["id"]
    schema = entry_section(section)
    async with request.form() as form:
        try:
            entries = await run_in_threadpool(save_form, user_id, schema, None, form)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
    return table_fragment(request, schema, entries, status_code=201)


@router.put("/{section}/{id}")
async def save_entry(request: Request, section: str, id: int):
    schema = entry_section(section)
    user_id = require_profile(request)["id"]
    async with request.form() as form:
        try:
            entries = await run_in_threadpool(save_form, user_id, schema, id, form)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
    return table_fragment(request, schema, entries)


@router.delete("/{section}/{id}")
def delete_entry(request: Request, section: str, id: int):
    user_id = require_profile(request)["id"]
    schema = entry_section(section)
    with write_lock, closing(connect()) as db:
        user = profile_user(db, user_id)
        require_row(db, schema, id, user_id)
        delete_row(db, schema, id, user_id)
        delete_entry_dir(user["username"], section, id)
        entries = list_rows(db, schema, user_id)
    return table_fragment(request, schema, entries)


@router.post("/{section}/reorder")
async def reorder_entries(request: Request, section: str):
    schema = entry_section(section)
    user_id = require_profile(request)["id"]
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "请求格式错误"}, status_code=400)
    ids = body.get("ids") if isinstance(body, dict) else None
    if not isinstance(ids, list):
        return JSONResponse({"error": "请求格式错误"}, status_code=400)
    try:
        entries = await run_in_threadpool(reorder_entry_rows, user_id, schema, ids)
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    return table_fragment(request, schema, entries)
