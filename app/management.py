from contextlib import closing

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import RedirectResponse

from .auth import require_admin
from .db import (
    connect, create_user, delete_user, get_ocr_settings, get_user, list_users,
    update_user, write_lock,
)
from .templating import templates

router = APIRouter(prefix="/admin/users")


def render_management(request: Request, *, error=None, status_code=200):
    require_admin(request)
    with closing(connect()) as db:
        users = list_users(db)
        settings = get_ocr_settings(db)
    return templates.TemplateResponse(request, "management.html", {
        "users": users,
        "ocr_settings": {"endpoint": settings["endpoint"], "model": settings["model"],
                         "has_api_key": bool(settings["api_key"])},
        "error": error, "saved": request.query_params.get("saved") == "1",
    }, status_code=status_code)


def active_admin(request, db):
    actor = require_admin(request)
    stored = get_user(db, actor["id"])
    if stored is None or stored["role"] != "admin" or stored["session_version"] != actor["session_version"]:
        request.session.clear()
        raise HTTPException(401, "会话已失效，请重新登录")
    return stored


@router.get("")
def management_page(request: Request):
    return render_management(request)


@router.post("")
async def add_user(request: Request):
    require_admin(request)
    async with request.form() as form:
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
    try:
        with write_lock, closing(connect()) as db:
            active_admin(request, db)
            create_user(db, username, password)
    except ValueError as error:
        return render_management(request, error=str(error), status_code=400)
    return RedirectResponse("/admin?saved=1", status_code=303)


@router.post("/{user_id}/edit")
async def edit_user(user_id: int, request: Request):
    require_admin(request)
    async with request.form() as form:
        username = str(form.get("username", ""))
        password = str(form.get("password", "")) or None
    try:
        with write_lock, closing(connect()) as db:
            actor = active_admin(request, db)
            target = get_user(db, user_id)
            if target is None:
                raise ValueError("该用户已不存在，请刷新列表")
            updated = update_user(db, user_id, username, password)
        if actor["id"] == user_id and actor["session_version"] != updated["session_version"]:
            request.session.clear()
            return RedirectResponse("/login", status_code=303)
    except ValueError as error:
        return render_management(request, error=str(error), status_code=400)
    return RedirectResponse("/admin?saved=1", status_code=303)


@router.post("/{user_id}/delete")
async def remove_user(user_id: int, request: Request):
    require_admin(request)
    async with request.form() as form:
        confirmation = str(form.get("confirm_username", ""))
    try:
        with write_lock, closing(connect()) as db:
            active_admin(request, db)
            target = get_user(db, user_id)
            if target is None:
                raise ValueError("该用户已不存在，请刷新列表")
            if confirmation != target["username"]:
                raise ValueError("删除未确认：请完整输入要删除的用户名")
            delete_user(db, user_id)
    except ValueError as error:
        return render_management(request, error=str(error), status_code=400)
    return RedirectResponse("/admin?saved=1", status_code=303)
