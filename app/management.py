from contextlib import closing
import hashlib
import hmac
import json
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from .auth import require_session_user
from .config import SECRET_KEY
from .db import (
    connect, get_ocr_settings, get_user, save_ocr_settings, update_user,
    validate_username, write_lock,
)
from .ocr import check_model
from .passwords import verify_password
from .sections import ENTRY_SECTIONS
from .templating import templates

router = APIRouter(prefix="/settings")
ocr_test_signer = URLSafeTimedSerializer(SECRET_KEY, salt="ocr-settings-test-user-v1")


def active_session_user(request: Request, db) -> dict:
    """Revalidate a Cookie identity under the caller's write_lock."""
    actor = require_session_user(request)
    stored = get_user(db, actor["id"])
    if stored is None or stored["session_version"] != actor["session_version"]:
        request.session.clear()
        raise HTTPException(401, "会话已失效，请重新登录")
    return stored


def public_ocr_settings(settings: dict) -> dict:
    return {"endpoint": settings["endpoint"], "model": settings["model"],
            "has_api_key": bool(settings["api_key"])}


async def json_body(request: Request, allowed: set[str]) -> dict:
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("请求必须为 JSON 对象") from error
    if not isinstance(body, dict) or set(body) - allowed:
        raise ValueError("请求字段无效")
    return body


def candidate_ocr_settings(body: dict, stored: dict) -> dict[str, str]:
    if not isinstance(body, dict):
        raise ValueError("请求必须为 JSON 对象")
    endpoint, model = body.get("endpoint"), body.get("model")
    if not isinstance(endpoint, str) or not isinstance(model, str):
        raise ValueError("请填写完整接口地址和模型名")
    endpoint, model = endpoint.strip(), model.strip()
    if not endpoint or len(endpoint) > 2048 or not model or len(model) > 256:
        raise ValueError("接口地址或模型名长度无效")
    try:
        url = urlsplit(endpoint)
        if (url.scheme not in ("http", "https") or not url.hostname or url.fragment
                or url.username is not None or url.password is not None
                or any(char.isspace() for char in endpoint)):
            raise ValueError
        url.port
    except ValueError as error:
        raise ValueError("请填写有效的 HTTP(S) 接口地址") from error
    api_key = body.get("api_key")
    if api_key is None:
        api_key = stored["api_key"]
    if (not isinstance(api_key, str) or len(api_key) > 4096 or not api_key.isascii()
            or any(ord(char) < 32 or ord(char) == 127 for char in api_key)):
        raise ValueError("API Key 格式无效")
    return {"endpoint": endpoint, "model": model, "api_key": api_key.strip()}


def settings_fingerprint(settings: dict[str, str]) -> str:
    payload = json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(SECRET_KEY.encode(), payload, hashlib.sha256).hexdigest()


def settings_error(error: ValueError | HTTPException) -> JSONResponse:
    if isinstance(error, HTTPException):
        return JSONResponse({"error": error.detail}, status_code=error.status_code)
    return JSONResponse({"error": str(error)}, status_code=400)


@router.get("")
def management_page(request: Request):
    try:
        with write_lock, closing(connect()) as db:
            user = active_session_user(request, db)
            settings = get_ocr_settings(db, user["id"])
        return templates.TemplateResponse(request, "management.html", {
            "user": {key: user[key] for key in ("id", "username", "session_version")},
            "ocr_settings": public_ocr_settings(settings), "active_nav": "settings",
            "section_labels": {section.key: section.label for section in ENTRY_SECTIONS},
        })
    except HTTPException as error:
        return settings_error(error)


def save_account(request: Request, body: dict) -> dict:
    for field in ("current_password", "username", "new_password", "confirm_password"):
        if not isinstance(body.get(field, ""), str):
            raise ValueError("账号字段必须为文本")
    current_password = body.get("current_password", "")
    username = body.get("username", "")
    new_password = body.get("new_password", "")
    validate_username(username)
    if new_password and (not 8 <= len(new_password) <= 256
                         or new_password != body.get("confirm_password", "")):
        raise ValueError("新密码须为 8–256 个字符且两次输入一致")
    with write_lock, closing(connect()) as db:
        actor = active_session_user(request, db)
        if not verify_password(current_password, actor["password_hash"]):
            raise ValueError("当前密码错误")
        password = new_password or None
        if password is not None and verify_password(password, actor["password_hash"]):
            password = None
        if username == actor["username"] and password is None:
            return {"reauthenticate": False}
        update_user(db, actor["id"], username, password)
        request.session.clear()
    return {"reauthenticate": True, "login_url": "/login"}


@router.post("/account")
async def account_settings(request: Request):
    try:
        require_session_user(request)
        body = await json_body(request, {"current_password", "username", "new_password", "confirm_password"})
        return await run_in_threadpool(save_account, request, body)
    except (ValueError, HTTPException) as error:
        return settings_error(error)


@router.get("/model")
def model_settings(request: Request):
    try:
        with write_lock, closing(connect()) as db:
            actor = active_session_user(request, db)
            return public_ocr_settings(get_ocr_settings(db, actor["id"]))
    except HTTPException as error:
        return settings_error(error)


@router.post("/model/test")
async def test_model_settings(request: Request):
    try:
        require_session_user(request)
        body = await json_body(request, {"endpoint", "model", "api_key"})
        with write_lock, closing(connect()) as db:
            actor = active_session_user(request, db)
            candidate = candidate_ocr_settings(body, get_ocr_settings(db, actor["id"]))
        try:
            await run_in_threadpool(check_model, candidate)
        except Exception:
            # Upstream exceptions may contain request headers or response secrets.
            return JSONResponse({"error": "模型测试失败，请检查接口、密钥及视觉与 JSON 支持"}, status_code=400)
        with write_lock, closing(connect()) as db:
            actor = active_session_user(request, db)
            proof = ocr_test_signer.dumps({"user_id": actor["id"],
                                          "session_version": actor["session_version"],
                                          "fingerprint": settings_fingerprint(candidate)})
        return {"test_token": proof}
    except (ValueError, HTTPException) as error:
        return settings_error(error)


@router.put("/model")
async def update_model_settings(request: Request):
    try:
        require_session_user(request)
        body = await json_body(request, {"endpoint", "model", "api_key", "test_token"})
        with write_lock, closing(connect()) as db:
            actor = active_session_user(request, db)
            candidate = candidate_ocr_settings(body, get_ocr_settings(db, actor["id"]))
            token = body.get("test_token")
            if not isinstance(token, str):
                raise ValueError("请先测试当前模型配置")
            try:
                tested = ocr_test_signer.loads(token, max_age=600)
            except BadSignature as error:
                raise ValueError("模型测试凭证已失效，请重新测试") from error
            if (not isinstance(tested, dict)
                    or set(tested) != {"user_id", "session_version", "fingerprint"}
                    or type(tested["user_id"]) is not int
                    or type(tested["session_version"]) is not int
                    or tested["user_id"] != actor["id"]
                    or tested["session_version"] != actor["session_version"]
                    or not isinstance(tested["fingerprint"], str)
                    or not hmac.compare_digest(tested["fingerprint"], settings_fingerprint(candidate))):
                raise ValueError("模型配置或登录会话已变化，请重新测试")
            save_ocr_settings(db, actor["id"], candidate["endpoint"], candidate["model"], candidate["api_key"])
        return public_ocr_settings(candidate)
    except (ValueError, HTTPException) as error:
        return settings_error(error)
