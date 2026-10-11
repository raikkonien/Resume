from hashlib import sha256
from pathlib import Path

from fastapi.templating import Jinja2Templates

from .sections import format_value
from .names import matches_name

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = TEMPLATE_DIR.parent / "static"
ASSET_VERSIONS = {
    name: sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:16]
    for name in ("admin.js", "resume.js", "style.css")
}
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals["format_value"] = format_value
templates.env.globals["asset_versions"] = ASSET_VERSIONS
templates.env.globals["matches_name"] = matches_name
