"""Regression coverage for the NDJSON-streaming POST /api/entries/{section}/ocr endpoint.

`tests/test_ocr.py` covers `recognize()`'s progress/cancellation hooks directly;
this file covers the record-layer plumbing that turns those hooks into an HTTP
NDJSON stream (request validation, event framing, and disconnect handling) that
isn't otherwise exercised anywhere.
"""
import asyncio
import io
import json
import os
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("MCP_TOKEN", "test-mcp-token")

import pymupdf
from starlette.datastructures import UploadFile

from app import admin, db
from app.sections import SECTIONS
from support import http_client, isolated_store


def make_pdf_bytes(page_count):
    with pymupdf.open() as document:
        for index in range(page_count):
            document.new_page().insert_text(
                (72, 72), f"Contract page {index + 1}: research project information",
            )
        return document.tobytes()


def stub_model_response(fields=None):
    return json.dumps({"fields": fields or {}})


class OcrStreamEndpointTests(unittest.TestCase):
    def setUp(self):
        isolated_store(self)
        self.client = http_client(self)

    def events(self, response):
        return [json.loads(line) for line in response.text.splitlines() if line]

    def test_streams_one_progress_event_per_page_then_a_single_result(self):
        with patch("app.ocr._call_model", side_effect=lambda *a, **k: stub_model_response()):
            response = self.client.post(
                "/api/entries/projects/ocr",
                files={"file": ("contract.pdf", make_pdf_bytes(3), "application/pdf")},
                data={"field": "contract"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/x-ndjson")
        events = self.events(response)
        self.assertEqual([event["type"] for event in events], ["progress", "progress", "progress", "result"])
        self.assertEqual([event["page"] for event in events[:3]], [1, 2, 3])
        result = events[3]
        self.assertEqual((result["pages_read"], result["skipped_pages"]), (3, []))
        self.assertIn("fields", result)
        self.assertIn("warnings", result)

    def test_streams_a_single_error_event_with_no_progress_for_a_bad_upload(self):
        response = self.client.post(
            "/api/entries/projects/ocr",
            files={"file": ("contract.pdf", b"not actually a pdf", "application/pdf")},
            data={"field": "contract"},
        )
        self.assertEqual(response.status_code, 200)
        events = self.events(response)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertIsInstance(events[0]["error"], str)

    def test_attachment_only_fields_are_rejected_before_any_model_call(self):
        for section, field in (("projects", "fund_proof"), ("projects", "close_proof"),
                               ("books", "publishing_contract")):
            with self.subTest(section=section, field=field):
                with patch("app.ocr._call_model") as mocked_call:
                    response = self.client.post(
                        f"/api/entries/{section}/ocr",
                        files={"file": ("proof.pdf", make_pdf_bytes(1), "application/pdf")},
                        data={"field": field},
                    )
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.json())
                self.assertNotEqual(response.headers["content-type"], "application/x-ndjson")
                mocked_call.assert_not_called()

    def test_projects_contract_and_other_sections_remain_recognizable(self):
        with patch("app.ocr._call_model", side_effect=lambda *a, **k: stub_model_response()):
            projects_response = self.client.post(
                "/api/entries/projects/ocr",
                files={"file": ("contract.pdf", make_pdf_bytes(1), "application/pdf")},
                data={"field": "contract"},
            )
            transfers_response = self.client.post(
                "/api/entries/transfers/ocr",
                files={"file": ("proof.pdf", make_pdf_bytes(1), "application/pdf")},
                data={"field": "payment_proof"},
            )
            book_responses = [self.client.post(
                "/api/entries/books/ocr",
                files={"file": ("page.pdf", make_pdf_bytes(1), "application/pdf")},
                data={"field": field},
            ) for field in ("first_page", "title_page")]
        for response in (projects_response, transfers_response, *book_responses):
            self.assertEqual(response.status_code, 200)
            events = self.events(response)
            self.assertEqual(events[-1]["type"], "result")

    def test_missing_file_and_invalid_field_still_return_early_json_400(self):
        no_file = self.client.post("/api/entries/projects/ocr", data={"field": "contract"})
        self.assertEqual(no_file.status_code, 400)
        self.assertIn("error", no_file.json())

        bad_field = self.client.post(
            "/api/entries/projects/ocr",
            files={"file": ("contract.pdf", make_pdf_bytes(1), "application/pdf")},
            data={"field": "not_a_real_field"},
        )
        self.assertEqual(bad_field.status_code, 400)
        self.assertIn("error", bad_field.json())


class StreamRecognitionCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def wait_until(self, predicate, timeout=2.0, interval=0.005):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not predicate():
            if loop.time() > deadline:
                raise AssertionError("condition was not met in time")
            await asyncio.sleep(interval)

    async def test_disconnect_mid_stream_stops_before_the_next_page_and_cleans_up(self):
        pdf_bytes = make_pdf_bytes(3)
        stream = io.BytesIO(pdf_bytes)
        upload = UploadFile(filename="contract.pdf", file=stream)
        call_log = []
        proceed_with_first_call = threading.Event()

        class FakeFormCM:
            def __init__(self):
                self.exit_count = 0

            async def __aexit__(self, *args):
                self.exit_count += 1

        form_cm = FakeFormCM()

        def fake_call_model(client, settings, messages, *, should_stop=None):
            call_log.append(len(call_log) + 1)
            if len(call_log) == 1:
                # Block the worker thread here to prove an in-flight provider
                # call is allowed to finish rather than being force-killed.
                proceed_with_first_call.wait(timeout=5)
            return stub_model_response()

        received = []

        async def consume():
            async for line in admin.stream_recognition(
                form_cm, SECTIONS["projects"], upload,
                {"endpoint": "https://model.example/chat", "model": "vision"},
                None, None, "contract",
            ):
                received.append(json.loads(line))

        with patch("app.ocr._call_model", side_effect=fake_call_model):
            consumer_task = asyncio.create_task(consume())
            await self.wait_until(lambda: len(received) >= 1)
            self.assertEqual(received, [{"type": "progress", "page": 1}])

            consumer_task.cancel()
            # Give stream_recognition's `finally` a chance to flip `cancelled`
            # (it does that before it ever awaits the still-blocked worker)
            # so releasing the in-flight call below can't race past the check.
            await asyncio.sleep(0.05)
            proceed_with_first_call.set()
            with self.assertRaises(asyncio.CancelledError):
                await consumer_task

        # The in-flight call for page 1 was allowed to finish, but no further
        # page (and no terminal event) was ever produced after cancellation.
        self.assertEqual(call_log, [1])
        self.assertEqual(received, [{"type": "progress", "page": 1}])
        self.assertEqual(form_cm.exit_count, 1)
        self.assertFalse(stream.closed)


if __name__ == "__main__":
    unittest.main()
