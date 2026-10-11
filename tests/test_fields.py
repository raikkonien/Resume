import json
import unittest
from datetime import date
from unittest.mock import patch

from app.sections import SECTIONS, format_value, parse_form, serialize_row


class IntegerBoundaryTests(unittest.TestCase):
    def test_sqlite_integer_limit_is_preserved_without_float_rounding(self):
        maximum = 2**63 - 1
        result = parse_form(SECTIONS["books"], {"word_count": str(maximum)})
        self.assertEqual(result["word_count"], maximum)
        with self.assertRaises(ValueError):
            parse_form(SECTIONS["books"], {"word_count": str(maximum + 1)})

    def test_huge_integer_is_a_validation_error_not_an_overflow(self):
        with self.assertRaises(ValueError):
            parse_form(SECTIONS["books"], {"word_count": "9" * 400})


class PaperAuthorFieldTests(unittest.TestCase):
    def setUp(self):
        self.section = SECTIONS["papers"]
        self.authors = [
            {"name": "张瑞", "affiliation": "浙江大学；Université de Montréal",
             "is_first": True, "is_corresponding": True},
            {"name": "Zoë 王", "affiliation": "", "is_first": True, "is_corresponding": False},
            {"name": "李四", "affiliation": "研究院", "is_first": False, "is_corresponding": True},
        ]

    def test_form_preserves_unicode_order_and_independent_roles(self):
        values = parse_form(self.section, {
            "authors": json.dumps(self.authors, ensure_ascii=False),
            "volume": "32", "issue": "S1", "pages": "e1463",
            "rank": "2/3", "authorship": "共同第一作者",
            "cas_quartile": "二区", "jcr_quartile": "Q1",
        })
        self.assertEqual(values["authors"], self.authors)
        self.assertEqual([values[key] for key in ("volume", "issue", "pages")], ["32", "S1", "e1463"])
        self.assertEqual((values["rank"], values["authorship"]), ("2/3", "共同第一作者"))
        self.assertEqual((values["cas_quartile"], values["jcr_quartile"]), ("二区", "Q1"))

    def test_empty_authors_and_unset_quartiles(self):
        for raw in ("", "[]"):
            values = parse_form(self.section, {"authors": raw})
            self.assertEqual(values["authors"], [])
            self.assertIsNone(values["cas_quartile"])
            self.assertIsNone(values["jcr_quartile"])
        self.assertEqual(parse_form(self.section, {})["authors"], [])

    def test_malformed_authors_are_rejected_instead_of_coerced(self):
        author = self.authors[0]
        for value in (
            None, {}, "张瑞", [None], [1], [{}],
            [{**author, "name": ""}], [{**author, "name": 42}],
            [{**author, "affiliation": None}], [{**author, "affiliation": ["研究院"]}],
            [{**author, "is_first": "false"}], [{**author, "is_first": 1}],
            [{**author, "is_corresponding": None}], [{**author, "unexpected": "ignored?"}],
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_form(self.section, {"authors": json.dumps(value)})
        with self.assertRaises(ValueError):
            parse_form(self.section, {"authors": "[broken"})
        with self.assertRaises(ValueError):
            parse_form(self.section, {"authors": self.authors})

    def test_export_text_is_complete_and_mcp_keeps_structured_authors(self):
        row = {"id": 7, "authors": self.authors}
        field = next(field for field in self.section.fields if field.key == "authors")
        text = format_value(field, row)
        for author in self.authors:
            self.assertIn(author["name"], text)
            if author["affiliation"]:
                self.assertIn(author["affiliation"], text)
        self.assertLess(text.index("张瑞"), text.index("Zoë 王"))
        self.assertLess(text.index("Zoë 王"), text.index("李四"))
        self.assertEqual(text.count("共同第一作者"), 2)
        self.assertEqual(text.count("共同通讯作者"), 2)
        self.assertEqual(serialize_row(self.section, row, "https://resume.test")["authors"], self.authors)
        self.assertEqual(serialize_row(self.section, {"id": 8}, "")["authors"], [])
        self.assertEqual(format_value(field, {"authors": None}), "—")


class AnnualReviewYearTests(unittest.TestCase):
    def test_year_choices_and_validation_advance_without_restart(self):
        section = SECTIONS["annual_reviews"]
        field = next(field for field in section.fields if field.key == "year")
        with patch("app.sections.date") as calendar:
            calendar.today.return_value = date(2026, 12, 31)
            self.assertEqual(field.choices, tuple(str(year) for year in range(2010, 2027)))
            self.assertEqual(parse_form(section, {"year": "2010"})["year"], "2010")
            self.assertEqual(parse_form(section, {"year": "2026"})["year"], "2026")
            for invalid in ("2009", "2027", "2026.0"):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    parse_form(section, {"year": invalid})
            calendar.today.return_value = date(2027, 1, 1)
            self.assertEqual(field.choices[-1], "2027")
            self.assertEqual(parse_form(section, {"year": "2027"})["year"], "2027")
            self.assertIsNone(parse_form(section, {"year": ""})["year"])


if __name__ == "__main__":
    unittest.main()
