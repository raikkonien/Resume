"""Render a profile snapshot in a separate process: MuPDF is not thread-safe."""
import base64
import io
import json
import re
import sys

import pymupdf
from PIL import Image, ImageOps
from markupsafe import Markup, escape

from .sections import BASIC, ENTRY_SECTIONS
from .templating import templates


def pdf_text(value: str) -> Markup:
    # MuPDF does not implement overflow-wrap. Break long identifiers without
    # inserting visible hyphens, and retain explicit multiline field content.
    value = re.sub(r"\S{25,}", lambda match: "\u200b".join(
        match[0][start:start + 24] for start in range(0, len(match[0]), 24)
    ), value)
    return Markup("<br>").join(escape(line) for line in value.splitlines())


def render_pdf(snapshot: dict) -> bytes:
    archive = pymupdf.Archive()
    portrait = None
    if snapshot["photo"]:
        with Image.open(io.BytesIO(base64.b64decode(snapshot["photo"]))) as source:
            image = ImageOps.exif_transpose(source)
            image.thumbnail((360, 480))
            with io.BytesIO() as output:
                image.save(output, format="PNG")
                archive.add(output.getvalue(), "portrait.png")
            scale = min(90 / image.width, 120 / image.height)
            portrait = {"width": image.width * scale, "height": image.height * scale}
    template = templates.env.get_template("resume_pdf.html")
    blocks = [template.render(basic_block=True, basic=snapshot["basic"],
                              basic_section=BASIC, portrait=portrait, pdf_text=pdf_text)]
    for section in ENTRY_SECTIONS:
        for index, row in enumerate(snapshot["rows"][section.key] or [None]):
            blocks.append(template.render(basic_block=False, section=section, row=row,
                                          position=index + 1, first=index == 0, pdf_text=pdf_text))
    page_rect = pymupdf.paper_rect("a4")
    content_rect = page_rect + (42, 42, -42, -48)
    headings = []
    page_number = 1

    def position(element):
        if element.id and element.id.startswith("section-") and element.open_close & 1:
            headings.append([1, element.text, page_number])

    with io.BytesIO() as output:
        writer = pymupdf.DocumentWriter(output)
        device = writer.begin_page(page_rect)
        top = content_rect.y0
        try:
            for html in blocks:
                # A zero-height placement can silently consume a Story. Start
                # a page before the remaining area is smaller than one line.
                if content_rect.y1 - top < 24:
                    writer.end_page()
                    device = writer.begin_page(page_rect)
                    page_number += 1
                    top = content_rect.y0
                story = pymupdf.Story(html, archive=archive)
                available = pymupdf.Rect(content_rect.x0, top, content_rect.x1, content_rect.y1)
                more, filled = story.place(available)
                # Keep ordinary records together. Oversized records can span
                # full pages; their text is never shrunk or truncated to fit.
                if (more or filled[3] > content_rect.y1) and top > content_rect.y0:
                    story.reset()
                    writer.end_page()
                    device = writer.begin_page(page_rect)
                    page_number += 1
                    available = content_rect
                    more, filled = story.place(available)
                while True:
                    bounds = pymupdf.Rect(filled)
                    if (bounds.x1 > content_rect.x1 + 1 or bounds.y1 > content_rect.y1 + 8
                            or (more and bounds.y1 <= available.y0)):
                        raise ValueError("PDF content does not fit the page")
                    story.element_positions(position)
                    story.draw(device)
                    if not more:
                        top = min(bounds.y1 + 8, content_rect.y1)
                        break
                    writer.end_page()
                    device = writer.begin_page(page_rect)
                    page_number += 1
                    available = content_rect
                    more, filled = story.place(available)
            writer.end_page()
        finally:
            writer.close()
        document = pymupdf.open(stream=output.getvalue(), filetype="pdf")
    with document:
        document.set_metadata({"title": f"{snapshot['basic'].get('name') or '个人'} · 简历"})
        document.set_toc(headings)
        for page in document:
            page.draw_line((42, page_rect.height - 36), (page_rect.width - 42, page_rect.height - 36),
                           color=(0.86, 0.89, 0.86), width=0.5)
            page.insert_textbox(pymupdf.Rect(42, page_rect.height - 29, page_rect.width - 42, page_rect.height - 14),
                                f"{page.number + 1} / {len(document)}", fontsize=8, align=2,
                                color=(0.38, 0.43, 0.40))
        document.subset_fonts()
        return document.tobytes(garbage=4, deflate=True)


if __name__ == "__main__":
    # No filenames, tokens or URLs are accepted from the browser; input is a
    # server-built snapshot and the archive contains only this profile's photo.
    pdf = render_pdf(json.load(sys.stdin))
    sys.stdout.buffer.write(pdf)
