"""Helpers that build small real ebook files for tests (epub, mobi, pdf).

Extracted from the catalogue tests when the store was removed, so the ingest and
enrichment tests that only needed these builders keep working.
"""
from __future__ import annotations

import struct
import zipfile
from pathlib import Path

import pymupdf


def make_epub(path: Path, title: str, author: str, isbn: str = "", body: str = "", lang: str = "en") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ident = f'<dc:identifier opf:scheme="ISBN">{isbn}</dc:identifier>' if isbn else ""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                   '</rootfiles></container>')
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
                   'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">'
                   f'<metadata><dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>{ident}'
                   f'<dc:language>{lang}</dc:language></metadata>'
                   '<manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>'
                   '<spine><itemref idref="c1"/></spine></package>')
        z.writestr("OEBPS/ch1.xhtml", f"<html><body><p>{body or (title + '. ') * 30}</p></body></html>")


def make_mobi(path: Path, title: str, author: str, isbn: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exth = b""
    recs = [(100, author), (503, title)] + ([(104, isbn)] if isbn else [])
    for t, v in recs:
        b = v.encode()
        exth += struct.pack(">II", t, len(b) + 8) + b
    exth = b"EXTH" + struct.pack(">II", len(exth) + 12, len(recs)) + exth
    hlen = 232
    mobi = bytearray(hlen)
    mobi[0:4] = b"MOBI"
    mobi[4:8] = struct.pack(">I", hlen)
    mobi[12:16] = struct.pack(">I", 65001)                        # r0[28:32]：UTF-8
    name = b"Short Name"
    r0 = bytearray(16) + mobi + exth
    fo = len(r0)
    r0 += name
    r0[84:92] = struct.pack(">II", fo, len(name))
    r0[128:132] = struct.pack(">I", 0x40)                          # 有 EXTH
    head = bytearray(78)
    head[0:len(title[:31])] = title[:31].encode()
    head[60:68] = b"BOOKMOBI"
    head[76:78] = struct.pack(">H", 1)
    data = bytes(head) + struct.pack(">I", 78 + 8) + b"\0" * 4 + bytes(r0)
    path.write_bytes(data)


def make_pdf(path: Path, text: str = "", title: str = "", author: str = "", pages: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for i in range(pages):
        pg = doc.new_page(width=300, height=400)
        if text:
            for k in range(8):
                pg.insert_text((20, 30 + 20 * k), f"{text} page {i} line {k} with enough words to count.", fontsize=8)
    doc.set_metadata({"title": title, "author": author})
    doc.save(path)
