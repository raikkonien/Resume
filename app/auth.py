from contextlib import closing
from contextvars import ContextVar
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse, RedirectResponse

from .db import connect, get_mcp_user, get_user, get_user_by_username
from .passwords import verify_password
from .templating import templates

router = APIRouter()
authenticated_user: ContextVar[dict | None] = ContextVar("authenticated_user", default=None)


def safe_next(value: str) -> str:
    if (value.startswith("/") and not value.startswith("//")
            and "\\" not in value and not any(ord(c) < 32 for c in value)):
        return value
    return "/"


def current_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "请重新登录")
    return user


def require_admin(request: Request) -> dict:
    user = current_user(request)
    if user["role"] != "admin" or getattr(request.state, "bearer", False):
        raise HTTPException(403, "仅管理员可管理用户和 OCR 设置")
    return user


def require_profile(request: Request) -> dict:
    user = current_user(request)
    if user["role"] != "user":
        raise HTTPException(403, "管理员没有个人档案")
    return user


def same_origin_write(request: Request) -> bool:
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return True  # Non-browser clients do not send Origin or ambient cookies.
    try:
        supplied = urlsplit(source)
        expected = urlsplit(str(request.url))
        return (supplied.scheme, supplied.hostname, supplied.port or (443 if supplied.scheme == "https" else 80)) == (
            expected.scheme, expected.hostname, expected.port or (443 if expected.scheme == "https" else 80))
    except ValueError:
        return False


@router.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {
        "next": safe_next(request.query_params.get("next", "/")),
    })


@router.post("/login")
async def login(request: Request):
    async with request.form() as form:
        destination = safe_next(str(form.get("next", "/")))
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
    request.session.clear()
    with closing(connect()) as db:
        user = get_user_by_username(db, username)
    if user is not None and verify_password(password, user["password_hash"]):
        request.session.update(user_id=user["id"], session_version=user["session_version"])
        if user["role"] == "admin":
            destination = "/admin"
        return RedirectResponse(destination, status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "next": destination, "username": username, "error": "用户名或密码错误",
    }, status_code=401)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = scope["path"]
        mcp_request = path == "/mcp" or path.startswith("/mcp/")
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        bearer = scheme.lower() == "bearer"
        if request.method not in ("GET", "HEAD", "OPTIONS") and not (mcp_request and bearer):
            if not same_origin_write(request):
                await JSONResponse({"error": "不允许跨站提交"}, status_code=403)(scope, receive, send)
                return
        request.state.user = None
        request.state.bearer = False
        if path in ("/login", "/logout", "/health") or path.startswith("/static/"):
            await self.app(scope, receive, send)
            return
        user = None
        with closing(connect()) as db:
            if bearer:
                if mcp_request or (request.method == "GET" and path.startswith("/files/")):
                    user = get_mcp_user(db, token)
                    request.state.bearer = user is not None
            elif not mcp_request:
                user_id = request.session.get("user_id")
                version = request.session.get("session_version")
                if type(user_id) is int and type(version) is int:
                    user = get_user(db, user_id)
                    if user is not None and user["session_version"] != version:
                        user = None
                if user is None:
                    request.session.clear()
        if user is not None:
            user = {key: user[key] for key in ("id", "username", "role", "session_version")}
            request.state.user = user
            context_token = authenticated_user.set(user)
            try:
                await self.app(scope, receive, send)
            finally:
                authenticated_user.reset(context_token)
            return
        if (not mcp_request and request.method == "GET"
                and "text/html" in request.headers.get("accept", "")
                and "x-requested-with" not in request.headers):
            response = RedirectResponse("/login?next=" + quote(path, safe=""), status_code=303)
        else:
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
        await response(scope, receive, send)
