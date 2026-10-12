from dataclasses import dataclass
from datetime import date
import json
import math
import re
from typing import Any, Mapping


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    type: str
    options: tuple[str, ...] = ()
    accept: str = ""
    date_format: str = ""
    year_from: int | None = None

    @property
    def choices(self) -> tuple[str, ...]:
        if self.year_from is not None:
            return tuple(str(year) for year in range(self.year_from, date.today().year + 1))
        return self.options


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    fields: tuple[Field, ...]
    single: bool = False
    title_field: str = ""
    year_fields: tuple[str, ...] = ()


IMG = ".jpg,.jpeg,.png"
DOC = ".pdf,.jpg,.jpeg,.png"

BASIC = Section("basic", "个人基本信息", (
    Field("name", "姓名", "text"),
    Field("pinyin", "拼音", "text"),
    Field("gender", "性别", "select", ("男", "女")),
    Field("id_number", "身份证号", "text"),
    Field("native_place", "籍贯", "text"),
    Field("ethnicity", "民族", "text"),
    Field("birth_month", "出生年月", "month"),
    Field("political_status", "政治面貌", "select", (
        "中共党员", "中共预备党员", "共青团员", "民革党员", "民盟盟员", "民建会员",
        "民进会员", "农工党党员", "致公党党员", "九三学社社员", "台盟盟员", "无党派人士", "群众",
    )),
    Field("employer", "工作单位", "text"),
    Field("department", "所在部门", "text"),
    Field("position", "职务", "text"),
    Field("title", "职称", "text"),
    Field("specialty", "从事专业及专长", "textarea"),
    Field("phone", "联系电话", "text"),
    Field("mobile", "手机号码", "text"),
    Field("email", "Email", "text"),
    Field("address", "单位地址", "text"),
    Field("postcode", "邮政编码", "text"),
    Field("photo", "照片", "file", accept=IMG),
), single=True, title_field="name")

ENTRY_SECTIONS = (
    Section("work", "工作与职称", (
        Field("begin_date", "开始时间", "date"),
        Field("end_date", "结束时间", "date"),
        Field("employer", "工作单位", "text"),
        Field("title", "取得职称", "text"),
    ), title_field="employer", year_fields=("begin_date",)),
    Section("contracts", "合同", (
        Field("identifier", "合同标识", "select", ("订立", "续订")),
        Field("type", "合同类型", "select", ("聘用合同",)),
        Field("begin_date", "合同起始日期", "date", date_format="YYYY.MM.DD"),
        Field("end_date", "合同终止日期", "date", date_format="YYYY.MM.DD"),
        Field("terms", "合同约定事项", "textarea"),
        Field("contract_pdf", "合同 PDF", "file", accept=".pdf"),
    ), title_field="type", year_fields=("begin_date",)),
    Section("annual_reviews", "年度考核", (
        Field("category", "考核类别", "select", ("年度考核",)),
        Field("year", "考核年度", "select", year_from=2010),
        Field("grade", "绩效考核等级", "select", ("合格", "优秀")),
        Field("ungraded_reason", "参加考核不定等次原因", "text"),
        Field("absent_reason", "未参加考核原因", "text"),
        Field("advanced_type", "先进类型", "select", ("校级先进", "院级先进")),
    ), title_field="year", year_fields=("year",)),
    Section("appointments", "党政职务", (
        Field("department", "任职部门", "text"),
        Field("status", "当前任职状况", "select", ("在任", "不在任")),
        Field("name", "职务名称", "select", ("科长", "副科长")),
        Field("level", "职务级别", "select", ("正科级", "副科级", "正处级", "副处级")),
        Field("begin_date", "任现职时间", "date", date_format="YYYY.MM.DD"),
        Field("end_date", "免职日期", "date", date_format="YYYY.MM.DD"),
    ), title_field="name", year_fields=("begin_date",)),
    Section("positions", "岗位设置", (
        Field("level", "岗位级别", "text"),
        Field("begin_date", "起聘时间", "date", date_format="YYYY.MM.DD"),
        Field("employer", "聘任单位", "select", ("浙江大学",)),
        Field("change_type", "变动类型", "select", ("聘岗", "职称")),
    ), title_field="level", year_fields=("begin_date",)),
    Section("education", "学习", (
        Field("begin_date", "开始时间", "date"),
        Field("end_date", "结束时间", "date"),
        Field("school", "学校", "text"),
        Field("major", "所学专业", "text"),
        Field("degree", "获得学位", "text"),
        Field("diploma", "获得学历", "text"),
        Field("award_year", "授予年份", "year"),
        Field("supervisor", "导师", "text"),
        Field("full_time", "是否全日制", "checkbox"),
        Field("degree_cert", "学位证书", "file", accept=DOC),
        Field("diploma_cert", "学历证书", "file", accept=DOC),
    ), title_field="school", year_fields=("award_year",)),
    Section("papers", "文章", (
        Field("year", "年份", "year"),
        Field("journal", "杂志", "text"),
        Field("title", "文章题目", "text"),
        Field("rank", "本人排名", "rank"),
        Field("authorship", "本人署名标注", "select", (
            "唯一第一作者", "共同第一作者", "唯一通讯作者", "共同通讯作者", "作者署名按姓氏排序", "其他情况",
        )),
        Field("doi", "DOI 号", "text"),
        Field("volume", "卷号", "text"),
        Field("issue", "期号", "text"),
        Field("pages", "页码或文章号", "text"),
        Field("cas_quartile", "中科院分区", "select", ("一区", "二区", "三区", "四区")),
        Field("jcr_quartile", "JCR 分区", "select", ("Q1", "Q2", "Q3", "Q4")),
        Field("authors", "作者", "authors"),
        Field("article_pdf", "文章附件", "file", accept=DOC),
    ), title_field="title", year_fields=("year",)),
    Section("patents", "专利", (
        Field("region", "专利地", "select", ("国内", "国际")),
        Field("category", "专利类别", "select", ("发明专利", "实用新型专利", "外观设计专利")),
        Field("name", "专利名称", "text"),
        Field("number", "专利号", "text"),
        Field("holder", "专利权人", "text"),
        Field("inventors", "发明人", "text"),
        Field("grant_number", "授权公告号", "text"),
        Field("grant_date", "授权公告日期", "date"),
        Field("apply_date", "专利申请日期", "date"),
        Field("rank", "本人排名", "rank"),
        Field("certificate", "专利证书", "file", accept=DOC),
    ), title_field="name", year_fields=("grant_date",)),
    Section("software", "软件著作", (
        Field("year", "年份", "year"),
        Field("name", "软件名称", "text"),
        Field("holder", "著作权人", "text"),
        Field("reg_number", "登记号", "text"),
        Field("reg_date", "登记日期", "date"),
        Field("cert_number", "证书号", "text"),
        Field("rank", "本人排名", "rank"),
        Field("certificate", "软件著作证书", "file", accept=DOC),
    ), title_field="name", year_fields=("year",)),
    Section("projects", "课题", (
        Field("begin_date", "开始时间", "date"),
        Field("end_date", "结束时间", "date"),
        Field("name", "项目名称", "text"),
        Field("grant_number", "批准号", "text"),
        Field("category", "项目类别", "text"),
        Field("status", "项目状态", "select", ("在研", "已结题", "已终止")),
        Field("role", "项目参与状态", "select", ("主持", "参与")),
        Field("rank", "排名", "rank"),
        Field("funder", "资助机构", "text"),
        Field("amount", "资助金额（万元）", "number"),
        Field("close_date", "结题日期", "date"),
        Field("contract", "合同", "file", accept=DOC),
        Field("fund_proof", "经费到账证明", "file", accept=DOC),
        Field("close_proof", "结题证明", "file", accept=DOC),
    ), title_field="name", year_fields=("begin_date",)),
    Section("transfers", "成果转化", (
        Field("name", "成果名称", "text"),
        Field("type", "成果类型", "select", ("专利", "软著")),
        Field("form", "转化形式", "select", ("技术转让", "技术许可", "技术作价投资")),
        Field("amount", "成交金额（万元）", "number"),
        Field("company", "受让、许可或合作公司", "text"),
        Field("sign_date", "合同签订日期", "date"),
        Field("contract", "合同", "file", accept=DOC),
        Field("payment_proof", "到账证明", "file", accept=DOC),
    ), title_field="name", year_fields=("sign_date",)),
    Section("books", "论著", (
        Field("title", "专著题目", "text"),
        Field("editor", "编辑", "text"),
        Field("isbn", "ISBN 号", "text"),
        Field("country", "国家或地区", "text"),
        Field("pub_status", "出版状态", "select", ("已出版", "待出版")),
        Field("language", "语言", "select", ("中文", "外文")),
        Field("word_count", "总字数", "int"),
        Field("publisher", "出版社", "text"),
        Field("pub_date", "出版日期", "date"),
        Field("authors", "作者", "text"),
        Field("first_page", "书首页", "file", accept=DOC),
        Field("title_page", "书扉页", "file", accept=DOC),
        Field("publishing_contract", "出版合同", "file", accept=DOC),
    ), title_field="title", year_fields=("pub_date",)),
    Section("memberships", "社会兼职", (
        Field("society", "学协会名称", "text"),
        Field("committee", "专委会名称", "text"),
        Field("term", "届数", "text"),
        Field("role", "任职岗位", "select", ("主委", "常务副主委", "副主委", "常委", "委员")),
        Field("begin_month", "聘期开始", "month"),
        Field("end_month", "聘期结束", "month"),
        Field("certificate", "证书", "file", accept=DOC),
    ), title_field="society", year_fields=("begin_month",)),
    Section("standards", "团体/行业标准", (
        Field("name", "标准名称", "text"),
        Field("category", "标准类别", "select", ("团体标准", "行业标准")),
        Field("number", "编号", "text"),
        Field("publish_date", "发布日期", "date"),
        Field("rank", "本人排名", "rank"),
        Field("document", "标准文件", "file", accept=DOC),
    ), title_field="name", year_fields=("publish_date",)),
    Section("trainings", "培训学习", (
        Field("begin_date", "开始时间", "date"),
        Field("end_date", "结束时间", "date"),
        Field("organizer", "培训单位", "text"),
        Field("content", "培训内容", "textarea"),
        Field("certificate", "证书", "file", accept=DOC),
    ), title_field="content", year_fields=("begin_date",)),
    Section("conferences", "学术会议", (
        Field("type", "学术会议类型", "select", ("参加国内学术会议", "参加国际学术会议")),
        Field("name", "会议名称", "text"),
        Field("begin_date", "会议开始日期", "date"),
        Field("end_date", "会议结束日期", "date"),
        Field("country", "会议国家", "text"),
        Field("city", "会议城市", "text"),
        Field("organizer", "主办单位", "text"),
        Field("submission", "会议投稿", "file", accept=DOC),
        Field("certificate", "会议相关证书", "file", accept=DOC),
    ), title_field="name", year_fields=("begin_date",)),
    Section("licenses", "执业资格", (
        Field("name", "职业资格名称", "text"),
        Field("begin_date", "开始日期", "date"),
        Field("validity", "有效期", "text"),
        Field("issuer", "颁发单位", "text"),
        Field("certificate", "证书", "file", accept=DOC),
    ), title_field="name", year_fields=("begin_date",)),
)
SECTIONS = {section.key: section for section in (BASIC, *ENTRY_SECTIONS)}


def event_year(section: Section, row: Mapping[str, Any]) -> int | None:
    """Return the first valid business year, never an entry's storage timestamp."""
    fields = {field.key: field for field in section.fields}
    for key in section.year_fields:
        field = fields[key]
        value = row.get(key)
        if isinstance(value, bool):
            continue
        try:
            if field.type == "year" or (field.type == "select" and field.year_from is not None):
                if isinstance(value, int):
                    year = value
                elif isinstance(value, str) and re.fullmatch(r"[0-9]{4}", value):
                    year = int(value)
                else:
                    continue
            elif field.type == "date":
                if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                    continue
                year = date.fromisoformat(value).year
            elif field.type == "month":
                if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
                    continue
                year = date.fromisoformat(value + "-01").year
            else:
                continue
        except ValueError:
            continue
        if 1900 <= year <= 2100:
            return year
    return None


def record_label(section: Section, row: Mapping[str, Any]) -> str:
    if section.single:
        return "个人基本信息"
    value = row.get(section.title_field)
    title = str(value).strip() if value is not None else ""
    return (title or f"{section.label} #{row.get('id', '')}")[:120]


def validate_authors(value: Any) -> list[dict[str, Any]]:
    """Validate the ordered author value shared by forms and database writes."""
    if not isinstance(value, list):
        raise ValueError("作者格式错误，应为作者列表")
    authors = []
    keys = {"name", "affiliation", "is_first", "is_corresponding"}
    for author in value:
        if not isinstance(author, dict) or author.keys() != keys:
            raise ValueError("每位作者须包含姓名、单位、第一作者及通讯作者标记")
        if not isinstance(author["name"], str) or not author["name"].strip():
            raise ValueError("作者姓名不能为空")
        if not isinstance(author["affiliation"], str):
            raise ValueError("作者单位格式错误")
        if type(author["is_first"]) is not bool or type(author["is_corresponding"]) is not bool:
            raise ValueError("作者署名标记须为布尔值")
        authors.append({
            "name": author["name"].strip(),
            "affiliation": author["affiliation"].strip(),
            "is_first": author["is_first"],
            "is_corresponding": author["is_corresponding"],
        })
    return authors


def decode_authors(value: Any) -> list[dict[str, Any]]:
    """Read JSON TEXT or a public row's list; legacy SQL NULL means no authors."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ValueError("作者 JSON 格式错误") from None
    return validate_authors(value)


def parse_form(section: Section, form: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field in section.fields:
        if field.type == "file":
            continue
        raw = form.get(field.key, "")
        if not isinstance(raw, str):
            raise ValueError(f"{field.label}格式错误")
        value = raw.strip()
        if field.type == "checkbox":
            values[field.key] = int(value == "1")
        elif field.type == "authors":
            if not value:
                values[field.key] = []
            else:
                try:
                    authors = json.loads(value)
                except ValueError:
                    raise ValueError("作者 JSON 格式错误") from None
                values[field.key] = validate_authors(authors)
        elif not value:
            values[field.key] = None
        elif field.type == "date":
            try:
                values[field.key] = date.fromisoformat(value).isoformat()
            except ValueError:
                raise ValueError("日期格式错误") from None
        elif field.type == "month":
            if not re.fullmatch(r"\d{4}-\d{2}", value):
                raise ValueError("月份格式错误")
            values[field.key] = value
        elif field.type in ("year", "int", "number"):
            try:
                number = float(value) if field.type == "number" else int(value)
            except (ValueError, OverflowError):
                raise ValueError(f"{field.label}格式错误") from None
            if field.type == "number" and not math.isfinite(number):
                raise ValueError(f"{field.label}格式错误")
            if field.type == "year" and not 1900 <= number <= 2100:
                raise ValueError("年份应在 1900 至 2100 之间")
            if field.type != "year" and number < 0:
                raise ValueError(f"{field.label}不能为负数")
            if field.type == "int" and number > 2**63 - 1:
                raise ValueError(f"{field.label}超出整数范围")
            values[field.key] = number
        elif field.type == "select":
            if value not in field.choices:
                raise ValueError("选项无效")
            values[field.key] = value
        elif field.type == "rank":
            if not re.fullmatch(r"\d+/\d+", value):
                raise ValueError("排名格式应为“本人排名/总人数”，如 1/7")
            values[field.key] = value
        else:
            values[field.key] = value
    return values


def format_value(field: Field, row: Mapping[str, Any]) -> str:
    value = row.get(field.key)
    if field.type == "authors":
        authors = decode_authors(value)
        first_count = sum(author["is_first"] for author in authors)
        corresponding_count = sum(author["is_corresponding"] for author in authors)
        entries = []
        for author in authors:
            details = [author["affiliation"]] if author["affiliation"] else []
            if author["is_first"]:
                details.append("共同第一作者" if first_count > 1 else "第一作者")
            if author["is_corresponding"]:
                details.append("共同通讯作者" if corresponding_count > 1 else "通讯作者")
            entries.append(author["name"] + (f"（{'；'.join(details)}）" if details else ""))
        return "；".join(entries) or "—"
    if field.type == "checkbox":
        return "是" if value else "否"
    if value is None or value == "":
        return "—"
    if field.type == "date" and field.date_format == "YYYY.MM.DD":
        return str(value).replace("-", ".")
    if field.type == "number":
        return f"{value:.2f}"
    return str(value)


def serialize_row(section: Section, row: Mapping[str, Any], base_url: str) -> dict[str, Any]:
    result = {"id": row["id"]}
    for field in section.fields:
        value = row.get(field.key)
        if field.type == "file":
            result[f"{field.key}_url"] = (
                f"{base_url.rstrip('/')}/files/{section.key}/{row['id']}/{field.key}?download=1"
                if value else None
            )
            result[f"{field.key}_name"] = row.get(f"{field.key}_name")
        elif field.type == "authors":
            result[field.key] = decode_authors(value)
        else:
            result[field.key] = bool(value) if field.type == "checkbox" else value
    return result
