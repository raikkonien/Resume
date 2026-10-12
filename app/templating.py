from hashlib import sha256
from pathlib import Path

from fastapi.templating import Jinja2Templates

from .sections import event_year, format_value, record_label
from .names import matches_name

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = TEMPLATE_DIR.parent / "static"
ASSET_VERSIONS = {
    path.relative_to(STATIC_DIR).as_posix(): sha256(path.read_bytes()).hexdigest()[:16]
    for path in STATIC_DIR.rglob("*") if path.suffix in (".js", ".css")
}
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals["format_value"] = format_value
templates.env.globals["asset_versions"] = ASSET_VERSIONS
templates.env.globals["matches_name"] = matches_name
templates.env.globals["event_year"] = event_year
templates.env.globals["record_label"] = record_label


def cell_value(field, row):
    if field.type == "file":
        return row.get(field.key + "_name") if row.get(field.key) else None
    if field.type == "authors":
        return format_value(field, row) if row.get(field.key) else None
    if field.type == "checkbox":
        return bool(row.get(field.key))
    return row.get(field.key)


def cell_search(field, row):
    value = cell_value(field, row)
    return "" if value is None or value == "" else str(value) if field.type == "file" else format_value(field, row)


templates.env.globals["cell_value"] = cell_value
templates.env.globals["cell_search"] = cell_search
