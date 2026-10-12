import io
import json
import os
import unittest
from datetime import date
from contextlib import closing
from unittest.mock import patch

import httpx
import pymupdf
from PIL import Image
from starlette.datastructures import UploadFile

os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("MCP_TOKEN", "test-mcp-token")

from app.ocr import (
    RecognitionCancelled,
    _call_model,
    _ModelOutputTruncated,
    _clean_text,
    _derive_rank,
    _extract_participants,
    _document_pages,
    _normalize_fields,
    _normalize_checkbox,
    _normalize_date,
    _normalize_field,
    _normalize_month,
    _normalize_number,
    _normalize_year,
    _parse_model_json,
    recognize,
)
from app.sections import SECTIONS


class CleanTextTests(unittest.TestCase):
    def test_exact_placeholder_token_is_dropped(self):
        self.assertIsNone(_clean_text("无"))
        self.assertIsNone(_clean_text("  N/A  "))
        self.assertIsNone(_clean_text(""))

    def test_text_merely_containing_a_placeholder_substring_is_kept(self):
        self.assertEqual(_clean_text("无线电研究所"), "无线电研究所")


class CheckboxAndNumberZeroPreservationTests(unittest.TestCase):
    def test_checkbox_false_values_are_preserved_not_treated_as_missing(self):
        self.assertEqual(_normalize_checkbox(False), 0)
        self.assertEqual(_normalize_checkbox(0), 0)
        self.assertEqual(_normalize_checkbox("否"), 0)
        self.assertEqual(_normalize_checkbox("是"), 1)
        self.assertIsNone(_normalize_checkbox("说不清"))

    def test_numeric_zero_is_preserved_not_treated_as_missing(self):
        self.assertEqual(_normalize_number(0, "number"), 0.0)
        self.assertEqual(_normalize_number("0", "int"), 0)
        self.assertEqual(_normalize_number("0万元", "number"), 0.0)

    def test_negative_and_non_numeric_are_rejected(self):
        self.assertIsNone(_normalize_number("-5", "number"))
        self.assertIsNone(_normalize_number("大约十万", "number"))

    def test_amount_units_and_large_integers_do_not_lose_value(self):
        self.assertEqual(_normalize_number("100,000元", "number"), 10)
        self.assertEqual(_normalize_number("10万元", "number"), 10)
        self.assertEqual(_normalize_number(str(2**63 - 1), "int"), 2**63 - 1)
        self.assertIsNone(_normalize_number(str(2**63), "int"))
        self.assertIsNone(_normalize_number("12.5", "int"))


class DateMonthYearNormalizationTests(unittest.TestCase):
    def test_date_accepts_common_chinese_and_separator_variants(self):
        self.assertEqual(_normalize_date("2020年5月1日"), "2020-05-01")
        self.assertEqual(_normalize_date("2020/05/01"), "2020-05-01")
        self.assertEqual(_normalize_date("20200501"), "2020-05-01")
        self.assertEqual(_normalize_date("2020-05-01"), "2020-05-01")

    def test_date_rejects_calendar_invalid_values(self):
        self.assertIsNone(_normalize_date("2020-13-40"))
        self.assertIsNone(_normalize_date("不确定"))

    def test_month_accepts_chinese_variant_and_rejects_out_of_range(self):
        self.assertEqual(_normalize_month("2020年5月"), "2020-05")
        self.assertEqual(_normalize_month("2020-05"), "2020-05")
        self.assertIsNone(_normalize_month("2020年13月"))

    def test_year_extracts_embedded_four_digit_year_and_rejects_out_of_range(self):
        self.assertEqual(_normalize_year("2020年"), 2020)
        self.assertIsNone(_normalize_year("3000"))
        self.assertIsNone(_normalize_year("未注明"))
        self.assertIsNone(_normalize_year("2020-2021"))

    def test_business_dates_require_an_explicit_valid_model_value(self):
        for section_key, key, number_key in (
            ("software", "reg_date", "reg_number"),
            ("standards", "publish_date", "number"),
        ):
            fields = list(SECTIONS[section_key].fields)
            with self.subTest(section=section_key):
                values, warnings = _normalize_fields(
                    {"fields": {key: "2026年10月11日", number_key: "2024-REG-123"}},
                    fields, None, None,
                )
                self.assertEqual(values[key], "2026-10-11")
                values, warnings = _normalize_fields(
                    {"fields": {key: "2026-02-29", number_key: "2024-REG-123"}},
                    fields, None, None,
                )
                self.assertNotIn(key, values)
                self.assertIn(key, warnings)
                values, _ = _normalize_fields({"fields": {number_key: "2024-REG-123"}},
                                              fields, None, None)
                self.assertNotIn(key, values)


class NormalizeFieldSelectAndUnknownTypeTests(unittest.TestCase):
    def test_select_rejects_value_outside_options_with_warning(self):
        field = SECTIONS["patents"].fields[1]  # category: select
        value, warning = _normalize_field(field, "发明专利")
        self.assertEqual((value, warning), ("发明专利", None))
        value, warning = _normalize_field(field, "外星专利")
        self.assertIsNone(value)
        self.assertIsNotNone(warning)

    def test_dynamic_review_year_accepts_current_year_but_not_future_or_early_year(self):
        field = next(f for f in SECTIONS["annual_reviews"].fields if f.key == "year")
        self.assertEqual(_normalize_field(field, date.today().year), (str(date.today().year), None))
        for year in (2009, date.today().year + 1):
            value, warning = _normalize_field(field, year)
            self.assertIsNone(value)
            self.assertIsNotNone(warning)

    def test_file_and_rank_field_types_never_produce_a_value(self):
        certificate_field = next(f for f in SECTIONS["patents"].fields if f.type == "file")
        rank_field = next(f for f in SECTIONS["patents"].fields if f.type == "rank")
        self.assertEqual(_normalize_field(certificate_field, "somefile.pdf"), (None, None))
        self.assertEqual(_normalize_field(rank_field, "1/3"), (None, None))


class DeriveRankTests(unittest.TestCase):
    def test_partial_or_unreadable_list_never_shrinks_rank_denominator(self):
        self.assertEqual(_extract_participants({"participants": ["张三", "李四"]}), [])
        for names in (["张三", None, "李四"], ["张三", "", "李四"], ["张三", "等"],
                      ["张三", "未知"], ["张三", "…"]):
            with self.subTest(names=names):
                self.assertEqual(_extract_participants({
                    "participants": names, "participants_complete": True,
                }), [])
        names = _extract_participants({
            "participants": ["张三", "李四", "王五"], "participants_complete": True,
        })
        self.assertEqual(_derive_rank(names, "李四", "", "本人排名"), ("2/3", None))

    def test_uncertain_identity_never_produces_rank(self):
        for names, name, pinyin in (
            ([], "李四", ""),
            (["张三", "李四"], "", ""),
            (["张三", "李四"], "赵六", ""),
            (["张三", "张三"], "张三", ""),
            (["Alice Chen", "San Zhang"], "张三", ""),
            (["Alice Chen", "S. Zhang"], "张三", "Zhang San"),
            (["San Zhang", "Zhang San"], "张三", "Zhang San"),
            (["张三", "San Zhang"], "张三", "Zhang San"),
        ):
            with self.subTest(names=names, name=name, pinyin=pinyin):
                value, warning = _derive_rank(names, name, pinyin, "本人排名")
                self.assertIsNone(value)
                self.assertIsNotNone(warning)

    def test_chinese_name_matching_does_not_depend_on_pinyin(self):
        self.assertEqual(
            _derive_rank(["张 三", "李四"], "张三", "Unrelated Name", "本人排名"),
            ("1/2", None),
        )

    def test_full_pinyin_supports_name_order_and_given_name_spacing(self):
        for candidate, pinyin in (
            ("SAN ZHANG", "Zhang San"),
            ("Zhang San", "San Zhang"),
            ("Xiao-Ming Zhang", "Zhang Xiaoming"),
            ("Xiaoming Zhang", "Zhang Xiao Ming"),
        ):
            with self.subTest(candidate=candidate, pinyin=pinyin):
                self.assertEqual(
                    _derive_rank(["Alice Chen", candidate, "Bob Li"], "张三", pinyin, "本人排名"),
                    ("2/3", None),
                )


class AuthorshipEvidenceTests(unittest.TestCase):
    def test_personal_authorship_requires_name_matched_role_evidence(self):
        fields = list(SECTIONS["papers"].fields)
        for evidence in (None, {"name": "张三", "quote": "作者：王一、张三、李四"},
                         {"name": "王一", "quote": "通讯作者：王一"},
                         {"name": "S. Zhang", "quote": "Corresponding author: S. Zhang"}):
            with self.subTest(evidence=evidence):
                values, _ = _normalize_fields(
                    {"fields": {"authorship": "唯一通讯作者"}, "authorship_evidence": evidence},
                    fields, "张三", "Zhang San",
                )
                self.assertNotIn("authorship", values)
        for name, quote in (("张三", "通讯作者：张三"),
                            ("San Zhang", "Corresponding author: San Zhang")):
            with self.subTest(name=name):
                values, _ = _normalize_fields(
                    {"fields": {"authorship": "唯一通讯作者"},
                     "authorship_evidence": {"name": name, "quote": quote}},
                    fields, "张三", "Zhang San",
                )
                self.assertEqual(values["authorship"], "唯一通讯作者")


class PaperBibliographyTests(unittest.TestCase):
    def normalize(self, fields, complete=True):
        return _normalize_fields(
            {"fields": fields, "authors_complete": complete},
            list(SECTIONS["papers"].fields), "张瑞", "Zhang Rui",
        )

    def test_bibliography_preserves_author_order_affiliations_and_supported_roles(self):
        authors = [
            {"name": "张瑞", "affiliation": "浙江大学；研究院",
             "is_first": True, "is_corresponding": True,
             "first_evidence": "张瑞、王一为共同第一作者",
             "corresponding_evidence": "通讯作者：张瑞"},
            {"name": "王一", "affiliation": "大学医院",
             "is_first": True, "is_corresponding": False,
             "first_evidence": "张瑞、王一为共同第一作者"},
            {"name": "Zoë Li", "affiliation": "Université de Montréal",
             "is_first": False, "is_corresponding": True,
             "corresponding_evidence": "Correspondence: Zoë Li"},
        ]
        values, warnings = self.normalize({
            "authors": authors, "volume": 32, "issue": " S1 ", "pages": "e1463",
            "cas_quartile": "一区", "jcr_quartile": "Q1",
        })
        expected = [{key: author[key] for key in ("name", "affiliation", "is_first", "is_corresponding")}
                    for author in authors]
        self.assertEqual(values["authors"], expected)
        self.assertEqual((values["volume"], values["issue"], values["pages"]), ("32", "S1", "e1463"))
        self.assertNotIn("cas_quartile", values)
        self.assertNotIn("jcr_quartile", values)
        self.assertEqual(warnings, {})

    def test_order_alone_uncertain_flags_and_unexplained_symbols_never_mark_roles(self):
        values, warnings = self.normalize({"authors": [
            {"name": "张瑞", "affiliation": "", "is_first": True, "is_corresponding": True,
             "first_evidence": "作者：张瑞、王一", "corresponding_evidence": "*"},
            {"name": "王一", "affiliation": None, "is_first": "true", "is_corresponding": None,
             "first_evidence": "共同第一作者：王一"},
        ]}, complete=False)
        self.assertEqual(values["authors"], [
            {"name": "张瑞", "affiliation": "", "is_first": False, "is_corresponding": False},
            {"name": "王一", "affiliation": "", "is_first": False, "is_corresponding": False},
        ])
        self.assertIn("authors", warnings)
        self.assertNotIn("rank", values)
        self.assertNotIn("authorship", values)

    def test_missing_and_partial_authors_warn_without_placeholder_people(self):
        for raw in (None, "张瑞", [], [None], [{"name": "未知"}]):
            with self.subTest(raw=raw):
                values, warnings = self.normalize({"authors": raw})
                self.assertNotIn("authors", values)
                self.assertIn("authors", warnings)
        values, warnings = self.normalize({"authors": [
            {"name": "张瑞", "affiliation": "大学", "is_first": False, "is_corresponding": False},
            {"name": "et al.", "affiliation": "大学"},
        ]})
        self.assertEqual([author["name"] for author in values["authors"]], ["张瑞"])
        self.assertIn("authors", warnings)

    def test_recognize_retains_partial_author_warning_and_excludes_quartiles(self):
        with pymupdf.open() as document:
            document.new_page().insert_text((72, 72), "Paper title. Authors: Rui Zhang, Alice Chen.")
            document.new_page().insert_text((72, 72), "Later page must not supply affiliations.")
            data = document.tobytes()
        response = {
            "fields": {
                "title": "Paper title", "volume": "32", "issue": "3", "pages": "760–771",
                "authors": [{"name": "Rui Zhang", "affiliation": "", "is_first": True,
                             "is_corresponding": False}],
                "cas_quartile": "二区", "jcr_quartile": "Q2",
            },
            "authors_complete": False,
            "participants": ["Rui Zhang", "Alice Chen"], "participants_complete": True,
        }
        with io.BytesIO(data) as stream, patch(
            "app.ocr._call_model", return_value=json.dumps(response),
        ) as model:
            result = recognize(SECTIONS["papers"], UploadFile(filename="paper.pdf", file=stream),
                               {}, "张瑞", "Zhang Rui", "article_pdf")
        self.assertEqual(result["pages_read"], 1)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result["fields"]["rank"], "1/2")
        self.assertFalse(result["fields"]["authors"][0]["is_first"])
        self.assertEqual(result["fields"]["pages"], "760–771")
        self.assertNotIn("cas_quartile", result["fields"])
        self.assertNotIn("jcr_quartile", result["fields"])
        self.assertTrue(any("作者" in warning for warning in result["warnings"]))


class PdfFirstPageTests(unittest.TestCase):
    def test_readable_first_page_uses_text_and_excludes_later_pages(self):
        with pymupdf.open() as document:
            document.new_page().insert_text((72, 72), "First-page title and authors: Alice Chen, San Zhang")
            document.new_page().insert_text((72, 72), "Second-page conflicting title and authors: Bob Li")
            with closing(_document_pages(".pdf", document.tobytes())) as pages:
                text, image, source = next(pages)
                self.assertEqual(list(pages), [])
        self.assertEqual(source, "pdf_text")
        self.assertIn("First-page title", text)
        self.assertNotIn("Second-page", text)
        self.assertIsNone(image)

    def test_scanned_first_page_does_not_use_later_page_text(self):
        with Image.new("RGB", (2400, 3200), "white") as image:
            image.putpixel((1200, 1600), (0, 0, 0))
            stream = io.BytesIO()
            image.save(stream, format="PNG")
        with pymupdf.open() as document:
            page = document.new_page(width=1200, height=1600)
            page.insert_image(page.rect, stream=stream.getvalue())
            page.insert_text((72, 72), "1")
            document.new_page().insert_text((72, 72), "Later-page text must never become OCR input")
            with closing(_document_pages(".pdf", document.tobytes())) as pages:
                text, encoded, source = next(pages)
                self.assertEqual(list(pages), [])
        self.assertEqual((text, source), ("", "pdf_image"))
        with Image.open(io.BytesIO(encoded)) as rendered:
            self.assertEqual(rendered.format, "JPEG")
            self.assertLessEqual(max(rendered.size), 1600)


class ProjectContractTests(unittest.TestCase):
    def setUp(self):
        self.complete_fields = {
            "begin_date": "2026-01-01", "end_date": "2028-12-31",
            "name": "Research project", "grant_number": "G-2026",
            "category": "基础研究", "status": "在研", "role": "参与",
            "funder": "Research fund", "amount": 0,
        }
        self.participants = {
            "participants": ["李四", "张三"], "participants_complete": True,
        }

    def recognize_pages(self, responses, *, source_field="contract", section="projects", scanned=()):
        with pymupdf.open() as document:
            for index in range(len(responses)):
                page = document.new_page()
                if index not in scanned:
                    page.insert_text((72, 72), f"Contract page {index + 1}: research project information")
            data = document.tobytes()
        with io.BytesIO(data) as stream:
            upload = UploadFile(filename="contract.pdf", file=stream)
            with patch("app.ocr._call_model", side_effect=[
                json.dumps(response, ensure_ascii=False) for response in responses
            ]):
                return recognize(SECTIONS[section], upload, {}, "张三", "Zhang San", source_field)

    def test_later_pages_fill_gaps_without_overwriting_or_reading_past_completion(self):
        result = self.recognize_pages([
            {"fields": {"name": "Cover title", "begin_date": "not a date", "amount": 0}},
            {"fields": {**self.complete_fields, "amount": 99, "close_date": "2029-01-01"},
             **self.participants},
            {"fields": {"name": "Appendix title"}},
        ])
        self.assertEqual(result["fields"], {**self.complete_fields, "name": "Cover title", "rank": "2/2"})
        self.assertEqual(result["pages_read"], 2)
        self.assertEqual(result["warnings"], [])

    def test_first_page_completion_does_not_require_close_date_or_render_later_scan(self):
        result = self.recognize_pages([
            {"fields": self.complete_fields, **self.participants},
            {"fields": {"close_date": "2029-01-01"}},
        ], scanned=(1,))
        self.assertEqual(result["fields"], {**self.complete_fields, "rank": "2/2"})
        self.assertEqual((result["pages_read"], result["source"]), (1, "pdf_text"))

    def test_missing_rank_continues_until_complete_personnel_list(self):
        result = self.recognize_pages([
            {"fields": self.complete_fields, "participants": ["张三"], "participants_complete": False},
            {"fields": {}, **self.participants},
            {"fields": {}},
        ])
        self.assertEqual(result["fields"], {**self.complete_fields, "rank": "2/2"})
        self.assertEqual(result["pages_read"], 2)
        self.assertEqual(result["warnings"], [])

    def test_exhausted_mixed_document_preserves_partial_fields_and_ignores_close_date(self):
        result = self.recognize_pages([
            {"fields": {"name": "Cover title"}},
            {"fields": {}},
            {"fields": {"amount": "100000元", "close_date": "2029-01-01"}},
            {"fields": {"funder": "Research fund"}, **self.participants},
        ], scanned=(1, 2))
        self.assertEqual(result["fields"], {
            "name": "Cover title", "amount": 10, "funder": "Research fund", "rank": "2/2",
        })
        self.assertEqual((result["pages_read"], result["source"]), (4, "pdf_mixed"))
        self.assertEqual(result["warnings"], [])

    def test_other_sections_still_read_only_first_page_for_any_file_field(self):
        for section, source_field in (("transfers", "contract"), ("transfers", "payment_proof")):
            with self.subTest(section=section, source_field=source_field):
                result = self.recognize_pages([
                    {"fields": {"name": "Cover title"}},
                    {"fields": {"name": "Later title", "amount": 10}},
                ], section=section, source_field=source_field)
                self.assertEqual(result["pages_read"], 1)
                self.assertEqual(result["fields"]["name"], "Cover title")
                self.assertNotIn("amount", result["fields"])


class EmploymentContractTests(unittest.TestCase):
    def recognize_pages(self, responses, *, source_field="contract_pdf", scanned=(), **hooks):
        with pymupdf.open() as document:
            for index in range(len(responses)):
                page = document.new_page()
                if index not in scanned:
                    page.insert_text((72, 72), f"Employment contract page {index + 1}: terms and dates")
            data = document.tobytes()
        replies = [
            response if isinstance(response, Exception) else json.dumps(response, ensure_ascii=False)
            for response in responses
        ]
        with io.BytesIO(data) as stream:
            upload = UploadFile(filename="employment.pdf", file=stream)
            with patch("app.ocr._call_model", side_effect=replies) as model:
                result = recognize(
                    SECTIONS["contracts"], upload, {}, None, None, source_field, **hooks,
                )
                return result, model.call_args_list

    def test_later_pages_fill_fields_and_mixed_scan_without_singleton_defaults(self):
        result, calls = self.recognize_pages([
            {"fields": {}},
            {"fields": {"begin_date": "2026.10.06", "terms": "须完成约定岗位任务"}},
            {"fields": {"end_date": "2029年10月5日"}},
        ], scanned=(1,), source_field="")
        self.assertEqual(result["fields"], {
            "begin_date": "2026-10-06", "end_date": "2029-10-05", "terms": "须完成约定岗位任务",
        })
        self.assertEqual((result["pages_read"], result["source"]), (3, "pdf_mixed"))
        self.assertEqual(result["warnings"], [])
        self.assertTrue(any(part["type"] == "image_url" for part in calls[1].args[2][-1]["content"]))
        self.assertFalse(any(part["type"] == "image_url" for part in calls[2].args[2][-1]["content"]))

    def test_filled_first_page_still_reads_all_terms_and_preserves_confirmed_fields(self):
        complete = {
            "identifier": "订立", "type": "聘用合同", "begin_date": "2026-01-01",
            "end_date": "2028-12-31", "terms": "遵守岗位职责\n按期完成工作",
        }
        pages = []
        result, calls = self.recognize_pages([
            {"fields": complete},
            {"fields": {"identifier": "续订", "begin_date": "2030-01-01",
                        "terms": "遵守岗位职责\n完成年度考核"}},
            {"fields": {"terms": "按期 完成工作\n办理交接手续"}},
        ], on_page_start=pages.append)
        self.assertEqual(result["fields"], {
            **complete, "terms": "遵守岗位职责\n按期完成工作\n完成年度考核\n办理交接手续",
        })
        self.assertEqual(result["pages_read"], 3)
        self.assertEqual(pages, [1, 2, 3])
        self.assertEqual(len(calls), 3)

    def test_unconfirmed_values_remain_absent(self):
        result, _ = self.recognize_pages([
            {"fields": {"identifier": "未知", "type": "", "begin_date": "2026-10",
                        "end_date": None, "terms": "未提供"}},
            {"fields": {}},
        ])
        self.assertEqual(result["fields"], {})
        self.assertEqual(result["pages_read"], 2)

    def test_truncated_page_does_not_discard_other_pages_or_later_terms(self):
        result, _ = self.recognize_pages([
            {"fields": {"terms": "履行岗位职责"}},
            _ModelOutputTruncated("truncated after retry"),
            {"fields": {"terms": "遵守保密约定", "end_date": "2029-10-05"}},
        ])
        self.assertEqual(result["fields"], {
            "terms": "履行岗位职责\n遵守保密约定", "end_date": "2029-10-05",
        })
        self.assertEqual(result["skipped_pages"], [2])
        self.assertEqual(result["pages_read"], 3)
        self.assertTrue(result["warnings"])

    def test_all_truncated_pages_fail(self):
        with self.assertRaises(_ModelOutputTruncated):
            self.recognize_pages([_ModelOutputTruncated("first"), _ModelOutputTruncated("second")])

    def test_cancellation_after_first_page_stops_further_progress(self):
        pages = []
        with self.assertRaises(RecognitionCancelled):
            self.recognize_pages(
                [{"fields": {"terms": "履行岗位职责"}}, {"fields": {"terms": "不得继续识别"}}],
                on_page_start=pages.append, should_stop=lambda: bool(pages),
            )
        self.assertEqual(pages, [1])

    def test_wrong_source_and_non_pdf_are_rejected_before_model_call(self):
        with pymupdf.open() as document:
            document.new_page()
            pdf_data = document.tobytes()
        with Image.new("RGB", (32, 32), "white") as image, io.BytesIO() as image_stream:
            image.save(image_stream, format="PNG")
            image_data = image_stream.getvalue()
        cases = (
            ("certificate", "employment.pdf", pdf_data),
            ("contract_pdf", "employment.png", image_data),
            ("", "employment.png", image_data),
            ("contract_pdf", "employment.pdf", b"not a PDF"),
        )
        for source_field, filename, data in cases:
            with self.subTest(source_field=source_field, filename=filename):
                with io.BytesIO(data) as stream, patch("app.ocr._call_model") as model:
                    upload = UploadFile(filename=filename, file=stream)
                    with self.assertRaises(ValueError):
                        recognize(SECTIONS["contracts"], upload, {}, None, None, source_field)
                    model.assert_not_called()


class ProjectAttachmentEligibilityTests(unittest.TestCase):
    def test_non_contract_project_attachments_are_rejected_before_any_model_call(self):
        with pymupdf.open() as document:
            document.new_page().insert_text((72, 72), "Fund proof cover page with some readable text")
            data = document.tobytes()
        for source_field in ("fund_proof", "close_proof", ""):
            with self.subTest(source_field=source_field):
                with io.BytesIO(data) as stream:
                    upload = UploadFile(filename="proof.pdf", file=stream)
                    with patch("app.ocr._call_model") as mocked_call:
                        with self.assertRaises(ValueError):
                            recognize(SECTIONS["projects"], upload, {}, "张三", "Zhang San", source_field)
                        mocked_call.assert_not_called()


class RecognitionProgressAndCancellationTests(unittest.TestCase):
    def build_contract_pdf(self, page_count):
        with pymupdf.open() as document:
            for index in range(page_count):
                document.new_page().insert_text(
                    (72, 72), f"Contract page {index + 1}: research project information",
                )
            return document.tobytes()

    def page_of(self, request):
        content = json.loads(request.content)["messages"][-1]["content"]
        text = next(part["text"] for part in content if "Contract page " in part.get("text", ""))
        return int(text.split("Contract page ")[1].split(":")[0])

    def recognize_with_hooks(self, page_count, respond, **hooks):
        data = self.build_contract_pdf(page_count)
        with io.BytesIO(data) as stream, \
                closing(httpx.Client(transport=httpx.MockTransport(respond))) as client:
            upload = UploadFile(filename="contract.pdf", file=stream)
            with patch("app.ocr.httpx.Client", return_value=client):
                return recognize(
                    SECTIONS["projects"], upload,
                    {"endpoint": "https://model.example/chat", "model": "vision"},
                    "张三", "Zhang San", "contract", **hooks,
                )

    def test_progress_callback_fires_once_per_page_immediately_before_its_model_call(self):
        events = []

        def respond(request):
            events.append(("model_call", self.page_of(request)))
            return httpx.Response(200, json={"choices": [{
                "message": {"content": '{"fields": {}}'}, "finish_reason": "stop",
            }]})

        self.recognize_with_hooks(3, respond, on_page_start=lambda page: events.append(("progress", page)))
        self.assertEqual(events, [
            ("progress", 1), ("model_call", 1),
            ("progress", 2), ("model_call", 2),
            ("progress", 3), ("model_call", 3),
        ])

    def test_should_stop_before_first_page_never_calls_model(self):
        with self.assertRaises(RecognitionCancelled):
            self.recognize_with_hooks(2, lambda request: httpx.Response(500), should_stop=lambda: True)

    def test_should_stop_after_a_page_prevents_the_next_pages_model_call(self):
        called_pages = []

        def respond(request):
            called_pages.append(self.page_of(request))
            return httpx.Response(200, json={"choices": [{
                "message": {"content": '{"fields": {}}'}, "finish_reason": "stop",
            }]})

        with self.assertRaises(RecognitionCancelled):
            self.recognize_with_hooks(3, respond, should_stop=lambda: len(called_pages) >= 1)
        self.assertEqual(called_pages, [1])

    def test_should_stop_cancels_a_pending_truncation_retry_without_a_second_request(self):
        requests_seen = []

        def respond(request):
            requests_seen.append(json.loads(request.content)["max_tokens"])
            return httpx.Response(200, json={"choices": [{
                "message": {"content": '{"fields": {"name":'}, "finish_reason": "length",
            }]})

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            with self.assertRaises(RecognitionCancelled):
                _call_model(
                    client, {"endpoint": "https://model.example/chat", "model": "vision"}, [],
                    should_stop=lambda: len(requests_seen) >= 1,
                )
        self.assertEqual(requests_seen, [4096])


class ModelOutputLimitTests(unittest.TestCase):
    def test_truncated_reply_retries_with_room_for_complete_json(self):
        def respond(request):
            payload = json.loads(request.content)
            if payload["max_tokens"] < 10000:
                content, finish = '{"fields": {"name":', "length"
            else:
                content, finish = '{"fields": {"name": "Complete title"}}', "stop"
            return httpx.Response(200, json={"choices": [{
                "message": {"content": content}, "finish_reason": finish,
            }]})

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            result = _parse_model_json(_call_model(
                client, {"endpoint": "https://model.example/chat", "model": "vision"},
                [{"role": "user", "content": "Extract the project name."}],
            ))
        self.assertEqual(result["fields"], {"name": "Complete title"})

    def test_persistent_truncation_is_bounded_and_never_accepts_incomplete_output(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{
                "message": {"content": '{"fields": {"name": "Unreliable title"}}'},
                "finish_reason": "length",
            }]})

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            with self.assertRaises(ValueError):
                _call_model(client, {"endpoint": "https://model.example/chat", "model": "vision"}, [])
        self.assertEqual(len(requests), 2)


class TruncatedContractTests(unittest.TestCase):
    def recognize_with_model(self, handler, page_count):
        with pymupdf.open() as document:
            for index in range(page_count):
                document.new_page().insert_text((72, 72), f"Contract page {index + 1}: project details")
            data = document.tobytes()
        with io.BytesIO(data) as stream, closing(httpx.Client(transport=httpx.MockTransport(handler))) as client:
            upload = UploadFile(filename="contract.pdf", file=stream)
            with patch("app.ocr.httpx.Client", return_value=client):
                return recognize(
                    SECTIONS["projects"], upload,
                    {"endpoint": "https://model.example/chat", "model": "vision"},
                    "张三", "Zhang San", "contract",
                )

    def test_eighteen_page_contract_keeps_results_around_a_truncated_page(self):
        page_attempts = []

        def respond(request):
            content = json.loads(request.content)["messages"][-1]["content"]
            text = next(part["text"] for part in content if "Contract page " in part.get("text", ""))
            page = int(text.split("Contract page ")[1].split(":")[0])
            page_attempts.append(page)
            parsed = {"fields": {}}
            if page == 1:
                parsed["fields"] = {"name": "Cover title"}
            elif page == 2:
                parsed["fields"] = {"amount": 999, "grant_number": "UNTRUSTWORTHY"}
            elif page == 18:
                parsed = {
                    "fields": {"amount": "100000元", "funder": "Research fund"},
                    "participants": ["李四", "张三"], "participants_complete": True,
                }
            return httpx.Response(200, json={"choices": [{
                "message": {"content": json.dumps(parsed, ensure_ascii=False)},
                "finish_reason": "length" if page == 2 else "stop",
            }]})

        result = self.recognize_with_model(respond, 18)
        self.assertEqual(result["fields"], {
            "name": "Cover title", "amount": 10, "funder": "Research fund", "rank": "2/2",
        })
        self.assertEqual(result["pages_read"], 18)
        self.assertEqual(result["skipped_pages"], [2])
        self.assertEqual(page_attempts, [1, 2, 2, *range(3, 19)])

    def test_all_pages_truncated_reports_failure_instead_of_empty_success(self):
        def respond(request):
            return httpx.Response(200, json={"choices": [{
                "message": {"content": '{"fields": {"name": "Unreliable title"}}'},
                "finish_reason": "length",
            }]})

        with self.assertRaises(ValueError):
            self.recognize_with_model(respond, 2)



class ParseModelJsonTests(unittest.TestCase):
    def test_parses_fenced_json_block(self):
        content = '这里是结果：\n```json\n{"fields": {"name": "示例"}}\n```'
        self.assertEqual(_parse_model_json(content), {"fields": {"name": "示例"}})

    def test_parses_bare_object_with_surrounding_prose(self):
        content = 'Sure, here it is: {"fields": {"name": "示例"}} thanks'
        self.assertEqual(_parse_model_json(content), {"fields": {"name": "示例"}})

    def test_rejects_non_object_json(self):
        with self.assertRaises(ValueError):
            _parse_model_json("[1, 2, 3]")

    def test_rejects_malformed_json(self):
        with self.assertRaises(ValueError):
            _parse_model_json("not json at all")


if __name__ == "__main__":
    unittest.main()
