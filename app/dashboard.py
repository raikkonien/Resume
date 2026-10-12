"""Owner-scoped dashboard aggregates from one SQLite read snapshot."""
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import hashlib
import sqlite3
import unicodedata
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from .auth import require_session_user
from .db import connect, get_basic, list_rows
from .sections import ENTRY_SECTIONS, SECTIONS, event_year, record_label

router = APIRouter(prefix="/api")

_ORGANIZATION_FIELDS = {
    "work": "employer",
    "positions": "employer",
    "education": "school",
    "projects": "funder",
    "books": "publisher",
    "trainings": "organizer",
    "conferences": "organizer",
    "licenses": "issuer",
}


def _node(id, kind, label, *, section=None, entry_id=None, year=None, value=0):
    return {"id": id, "kind": kind, "label": label, "section": section,
            "entry_id": entry_id, "year": year, "value": value}


def _edge(source, target, kind, evidence):
    return {"source": source, "target": target, "kind": kind, "evidence": evidence}


def _text(value):
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _recent_href(event, active_ids):
    key = event["section_key"]
    section = SECTIONS.get(key)
    if event["action"] == "archive":
        return "/settings#archive"
    if section is None:
        return "/resume?tab=overview"
    if event["action"] == "reorder" or event["entry_id"] is None or section.single:
        return "/resume?" + urlencode({"tab": key})
    if event["entry_id"] not in active_ids[key]:
        return "/settings#archive"
    return "/resume?" + urlencode({"tab": key, "entry": event["entry_id"]})


def build_dashboard(db: sqlite3.Connection, user_id: int, *, today: date) -> dict[str, Any]:
    """Read once without writing or committing a caller's existing transaction.

    A savepoint starts a deferred read transaction on an idle connection. Its first
    SELECT fixes the snapshot, including while another WAL connection commits.
    An existing transaction keeps its own snapshot and remains open afterward.
    """
    db.execute("SAVEPOINT dashboard_snapshot")
    try:
        basic = get_basic(db, user_id)
        rows_by_section = {section.key: list_rows(db, section, user_id)
                           for section in ENTRY_SECTIONS}
        recent = [dict(row) for row in db.execute(
            "SELECT id, section_key, entry_id, action, title, occurred_at "
            "FROM record_events WHERE user_id = ? "
            "ORDER BY occurred_at DESC, id DESC LIMIT 10", (user_id,)
        )]
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT dashboard_snapshot")
        db.execute("RELEASE SAVEPOINT dashboard_snapshot")
        raise
    db.execute("RELEASE SAVEPOINT dashboard_snapshot")

    year = today.year
    years = [year - 2, year - 1, year]
    total = sum(len(rows) for rows in rows_by_section.values())
    nodes = [_node("self", "self", _text(basic.get("name")) or "个人履历", value=total)]
    edges = []
    sections = []
    year_counts = {}
    # (kind, normalized text) -> label and record -> concrete source fields.
    # Each record contributes once even when the same author appears repeatedly.
    relations = {}
    active_ids = {}

    def reference(kind, text, record_id, source):
        label = _text(text)
        if not label:
            return
        relation = relations.setdefault((kind, label.casefold()),
                                        {"label": label, "records": {}})
        sources = relation["records"].setdefault(record_id, {})
        sources[source] = None

    for section in ENTRY_SECTIONS:
        rows = rows_by_section[section.key]
        active_ids[section.key] = {row["id"] for row in rows}
        counts = [0, 0, 0]
        undated = 0
        section_id = f"section:{section.key}"
        if rows:
            nodes.append(_node(section_id, "section", section.label,
                               section=section.key, value=len(rows)))
            edges.append(_edge("self", section_id, "section", section.key))
        for row in rows:
            business_year = event_year(section, row)
            if business_year is None:
                undated += 1
            elif year - 2 <= business_year <= year:
                counts[business_year - (year - 2)] += 1
            record_id = f"record:{section.key}:{row['id']}"
            nodes.append(_node(record_id, "record", record_label(section, row),
                               section=section.key, entry_id=row["id"],
                               year=business_year, value=1))
            edges.append(_edge(section_id, record_id, "record",
                               f"{section.key}.{section.title_field}"))
            if business_year is not None:
                year_counts[business_year] = year_counts.get(business_year, 0) + 1
                # Reuse the shared parser to identify the actual priority source.
                source = next(key for key in section.year_fields
                              if event_year(section, {key: row.get(key)}) is not None)
                edges.append(_edge(record_id, f"year:{business_year}", "year",
                                   f"{section.key}.{source}"))
            if section.key == "papers":
                for index, author in enumerate(row.get("authors") or []):
                    reference("author", author.get("name"), record_id,
                              f"papers.authors[{index}].name")
                    reference("organization", author.get("affiliation"), record_id,
                              f"papers.authors[{index}].affiliation")
            field = _ORGANIZATION_FIELDS.get(section.key)
            if field is not None:
                reference("organization", row.get(field), record_id,
                          f"{section.key}.{field}")
        sections.append({"key": section.key, "label": section.label,
                         "current_count": counts[2], "counts": counts,
                         "undated_count": undated})

    for business_year, count in sorted(year_counts.items()):
        nodes.append(_node(f"year:{business_year}", "year", str(business_year),
                           year=business_year, value=count))
    for (kind, normalized), relation in relations.items():
        records = relation["records"]
        if len(records) < 2:
            continue
        relation_id = kind + ":" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        nodes.append(_node(relation_id, kind, relation["label"], value=len(records)))
        for record_id, sources in records.items():
            edges.append(_edge(record_id, relation_id, kind, ", ".join(sources)))
    for event in recent:
        event["href"] = _recent_href(event, active_ids)
    return {"year": year, "years": years,
            "current_total": sum(section["current_count"] for section in sections),
            "undated_total": sum(section["undated_count"] for section in sections),
            "sections": sections, "recent": recent,
            "graph": {"nodes": nodes, "edges": edges}}


@router.get("/dashboard")
def dashboard_data(request: Request):
    user = require_session_user(request)
    today = datetime.now(timezone(timedelta(hours=8))).date()
    try:
        with closing(connect()) as db:
            return JSONResponse(build_dashboard(db, user["id"], today=today),
                                headers={"Cache-Control": "no-store"})
    except Exception:
        # Database contents, paths and exception messages must not reach clients.
        return JSONResponse({"error": "首页数据暂时无法加载，请重试"}, status_code=500,
                            headers={"Cache-Control": "no-store"})
