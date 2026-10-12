"""Real authentication and isolated storage for HTTP regression tests."""
from contextlib import closing
from pathlib import Path
import tempfile
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from app import admin, auth, dashboard, db, management, view
from app.config import ADMIN_PASSWORD
from app.templating import STATIC_DIR

ADMIN_TEST_PASSWORD = "test-bootstrap-admin"


def isolated_store(case):
    root = Path(case.enterContext(tempfile.TemporaryDirectory()))
    case.enterContext(patch.object(db, "DATA_DIR", root))
    case.enterContext(patch.object(db, "DB_PATH", root / "resume.db"))
    case.enterContext(patch.object(db, "INITIAL_ADMIN_PASSWORD", ADMIN_TEST_PASSWORD))
    db.init_db()
    return root


def http_client(case, username="zhangrui", password=None):
    app = FastAPI()
    app.add_exception_handler(HTTPException, auth.http_error_response)
    app.add_middleware(auth.AuthMiddleware)
    app.add_middleware(SessionMiddleware, secret_key="isolated-test-session-key",
                       session_cookie="resume_session")
    app.include_router(auth.router)
    app.include_router(management.router)
    app.include_router(admin.router)
    app.include_router(dashboard.router)
    app.include_router(view.router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    client = case.enterContext(TestClient(app))
    if username:
        password = password or (ADMIN_TEST_PASSWORD if username == "admin" else ADMIN_PASSWORD)
        result = client.post("/login", data={"username": username, "password": password},
                             follow_redirects=False)
        case.assertEqual(result.status_code, 303, result.text)
    return client


def original_user_id():
    with closing(db.connect()) as conn:
        return db.get_user_by_username(conn, "zhangrui")["id"]
