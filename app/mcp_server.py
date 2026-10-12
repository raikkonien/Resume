from contextlib import closing
from typing import Any

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .auth import authenticated_user
from .config import MCP_ALLOWED_HOSTS, PUBLIC_BASE_URL
from .db import connect, get_basic, list_rows
from .sections import BASIC, ENTRY_SECTIONS, SECTIONS, Section, serialize_row

RO = ToolAnnotations(read_only_hint=True, open_world_hint=False)
mcp = MCPServer("resume", instructions=(
    "个人简历只读数据服务。先调用 list_sections 了解板块与字段，再调用 get_basic_info / list_<section> 或 get_full_resume。"
    "文件字段以 *_url 给出下载地址，请求时需携带与 MCP 相同的 Authorization: Bearer 头。"
    "金额单位为万元；排名格式 本人排名/总人数。"
    "文章的 authors 为按署名顺序排列的作者数组，每项含 name（姓名）、affiliation（单位）、"
    "is_first（是否第一作者）、is_corresponding（是否通信作者）；两种身份可同时为真。"
    "cas_quartile（中科院分区）与 jcr_quartile（JCR分区）由用户手工录入，空值表示未填写。"
    "日期以 YYYY-MM-DD 返回；合同、党政职务及岗位设置在网页/PDF 中显示为 YYYY.MM.DD。"
))


def profile_id() -> int:
    user = authenticated_user.get()
    if user is None:
        raise PermissionError("需要有效的个人资料令牌")
    return user["id"]


@mcp.tool(annotations=RO)
def list_sections() -> list[dict[str, Any]]:
    """列出全部板块及字段定义，顺序与维护页和展示页一致。"""
    return [{
        "key": section.key, "label": section.label, "single": section.single,
        "fields": [{"key": f.key, "label": f.label, "type": f.type, "options": list(f.choices)}
                   for f in section.fields],
    } for section in SECTIONS.values()]


@mcp.tool(annotations=RO)
def get_basic_info() -> dict[str, Any]:
    """读取个人基本信息，照片以带鉴权的下载地址提供。"""
    with closing(connect()) as db:
        return serialize_row(BASIC, get_basic(db, profile_id()), PUBLIC_BASE_URL)


def make_lister(section: Section):
    def lister() -> list[dict[str, Any]]:
        with closing(connect()) as db:
            return [serialize_row(section, row, PUBLIC_BASE_URL) for row in list_rows(db, section, profile_id())]
    return lister


for section in ENTRY_SECTIONS:
    mcp.tool(
        name=f"list_{section.key}",
        description=f"列出「{section.label}」全部条目，按维护页保存的自定义顺序。字段：" +
                    ", ".join(f"{f.key}({f.label})" for f in section.fields),
        annotations=RO,
    )(make_lister(section))


@mcp.tool(annotations=RO)
def get_full_resume() -> dict[str, Any]:
    """读取完整简历，包含基本信息与全部条目板块。"""
    with closing(connect()) as db:
        user_id = profile_id()
        db.execute("BEGIN")
        return {
            "basic": serialize_row(BASIC, get_basic(db, user_id), PUBLIC_BASE_URL),
            **{s.key: [serialize_row(s, row, PUBLIC_BASE_URL) for row in list_rows(db, s, user_id)]
               for s in ENTRY_SECTIONS},
        }


mcp_app = mcp.streamable_http_app(
    transport_security=TransportSecuritySettings(allowed_hosts=MCP_ALLOWED_HOSTS),
    json_response=True,
    stateless_http=True,
)
