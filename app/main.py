from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from . import admin, auth, management, view
from .config import SECRET_KEY
from .db import init_db
from .mcp_server import mcp, mcp_app
from .templating import STATIC_DIR


class CacheControlMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        policy = "no-cache" if scope["path"].startswith("/static/") else "no-store"

        async def send_response(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = policy
            await send(message)

        await self.app(scope, receive, send_response)


@asynccontextmanager
async def lifespan(app):
    init_db()
    async with mcp.session_manager.run():
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(auth.AuthMiddleware)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="resume_session",
                   max_age=14 * 24 * 3600, same_site="lax", https_only=False)
app.add_middleware(CacheControlMiddleware)
app.include_router(auth.router)
app.include_router(management.router)
app.include_router(admin.router)
app.include_router(view.router)


@app.get("/health")
def health():
    return {"status": "ok"}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/", mcp_app)
