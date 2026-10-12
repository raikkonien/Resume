import base64
from contextlib import closing
import json
import logging
import re
import subprocess
import sys
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import FileResponse, JSONResponse, Response

from .auth import current_user, require_session_user
from .db import connect, get_basic, get_row, get_table_layout, get_user, list_rows, write_lock
from .files import upload_path
from .sections import BASIC, ENTRY_SECTIONS, SECTIONS
from .templating import templates

router = APIRouter()


@router.get("/")
def home(request: Request):
    require_session_user(request)
    return templates.TemplateResponse(request, "dashboard.html", {"active_nav": "home"})


@router.get("/resume")
def resume(request: Request):
    user = require_session_user(request)
    with closing(connect()) as db:
        db.execute("BEGIN")
        basic = get_basic(db, user["id"])
        rows = {s.key: list_rows(db, s, user["id"]) for s in ENTRY_SECTIONS}
        layouts = {s.key: get_table_layout(db, s, user["id"]) for s in SECTIONS.values()}
    return templates.TemplateResponse(request, "resume.html", {
        "basic": basic, "basic_section": BASIC, "sections": ENTRY_SECTIONS,
        "all_sections": tuple(SECTIONS.values()), "rows": rows, "layouts": layouts,
        "active_nav": "resume",
    })


@router.get("/papers/{id}")
def paper_detail(request: Request, id: int):
    user = require_session_user(request)
    section = SECTIONS["papers"]
    with closing(connect()) as db:
        row = get_row(db, section, id, user["id"])
    if row is None:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "paper_detail.html", {
        "section": section, "row": row,
    }, headers={"Cache-Control": "no-store"})


@router.post("/resume/export")
def export_resume(request: Request):
    actor = require_session_user(request)
    try:
        with write_lock, closing(connect()) as db:
            db.execute("BEGIN")
            user = get_user(db, actor["id"])
            if user is None or user["session_version"] != actor["session_version"]:
                raise HTTPException(401, "请重新登录后导出")
            basic = get_basic(db, user["id"])
            rows = {section.key: list_rows(db, section, user["id"]) for section in ENTRY_SECTIONS}
            photo = base64.b64encode(upload_path(user["username"], basic["photo"]).read_bytes()).decode("ascii") if basic["photo"] else None
        snapshot = json.dumps({"basic": basic, "rows": rows, "photo": photo}, ensure_ascii=False).encode("utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "app.pdf_export"], input=snapshot,
            capture_output=True, check=True, timeout=90,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        logging.exception("Resume PDF export failed for user %s", actor["id"])
        return JSONResponse({"error": "PDF 生成失败，请稍后重试；若仍失败，请联系部署者检查资料和照片。"}, status_code=503)
    name = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', "_", basic["name"] or user["username"]).strip()[:80] or user["username"]
    filename = quote(f"{name}_简历.pdf", safe="")
    return Response(result.stdout, media_type="application/pdf", headers={
        "Content-Disposition": f"attachment; filename=\"resume.pdf\"; filename*=UTF-8''{filename}",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    })


@router.get("/files/{section}/{id}/{field}")
def file_response(request: Request, section: str, id: int, field: str):
    user = current_user(request)
    schema = SECTIONS.get(section)
    if schema is None or not any(f.key == field and f.type == "file" for f in schema.fields):
        raise HTTPException(404)
    with closing(connect()) as db:
        row = get_row(db, schema, id, user["id"])
    if row is None or not row[field]:
        raise HTTPException(404)
    try:
        path = upload_path(user["username"], row[field])
        if not path.is_file():
            raise HTTPException(404)
    except ValueError:
        raise HTTPException(404) from None
    return FileResponse(path, filename=row[f"{field}_name"],
                        content_disposition_type="attachment" if request.query_params.get("download") == "1" else "inline")
