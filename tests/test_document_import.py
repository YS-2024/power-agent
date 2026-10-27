import base64
import io
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from document_fixtures import docx_bytes, pdf_bytes
from power_agent.document_import import extract_document, parse_upload
from power_agent.knowledge import KnowledgeBase, located_chunks


class DocumentImportTests(unittest.TestCase):
    def test_pdf_page_locations_and_exact_extracted_lines(self):
        parsed = parse_upload("设备.PDF", base64.b64encode(pdf_bytes()).decode())
        self.assertFalse(parsed["warnings"])
        chunks = located_chunks(parsed["content"], "设备.pdf")
        self.assertEqual([c["location"] for c in chunks], ["PDF 第 1 页", "PDF 第 2 页"])
        self.assertIn("ZX-928", chunks[1]["text"])
        for chunk in chunks:
            self.assertEqual(chunk["text"], "\n".join(parsed["content"].splitlines()[chunk["start_line"]-1:chunk["end_line"]]))

    def test_docx_body_table_order_and_citations_survive_reopening(self):
        parsed = parse_upload("说明.docx", base64.b64encode(docx_bytes()).decode())
        text = parsed["content"]
        self.assertLess(text.index("DOCX-628"), text.index("TABLE-773"))
        self.assertLess(text.index("TABLE-773"), text.index("表后段落"))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "kb.db"
            kb = KnowledgeBase(path)
            added = kb.add("说明.docx", text, parsed=True)
            reopened = KnowledgeBase(path)
            source = reopened.search("TABLE-773")[0]
            self.assertEqual(source["document_id"], added["id"])
            self.assertEqual(source["location"], "DOCX 表格 1 第 2 行")
            self.assertTrue(reopened.add("说明.docx", text, parsed=True)["duplicate"])

    def test_empty_pdf_requires_ocr_and_mixed_pdf_warns(self):
        with self.assertRaisesRegex(ValueError, "OCR"):
            extract_document(pdf_bytes((None,)), ".pdf")
        parsed = extract_document(pdf_bytes((None, "Useful text")), ".pdf")
        self.assertIn("1", parsed["warnings"][0])
        self.assertIn("PDF 第 2 页", parsed["content"])

    def test_encryption_and_page_limit(self):
        with self.assertRaisesRegex(ValueError, "加密"):
            extract_document(pdf_bytes(encrypted=True), ".pdf")
        with self.assertRaisesRegex(ValueError, "200 页"):
            extract_document(pdf_bytes([None] * 201), ".pdf")

    def test_invalid_uploads(self):
        for name, data in [("x.doc", "YWJj"), ("x.pdf", "!invalid"), (None, "YWJj"),
                           ("x.pdf", ""), ("x.pdf", "YWJj"), ("x.docx", "YWJj"), ("x.pdf", 23)]:
            with self.subTest(name=name, data=data), self.assertRaises(ValueError):
                parse_upload(name, data)

    def test_docx_expanded_size_limit(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", "x" * 40_000_001)
        with self.assertRaisesRegex(ValueError, "解压后过大"):
            extract_document(output.getvalue(), ".docx")

    def test_extracted_text_and_input_limits(self):
        with patch("power_agent.document_import.MAX_TEXT_BYTES", 20):
            with self.assertRaisesRegex(ValueError, "提取后的文本"):
                extract_document(pdf_bytes(), ".pdf")
        with patch("power_agent.document_import.MAX_FILE_BYTES", 20):
            with self.assertRaisesRegex(ValueError, "10 MB"):
                parse_upload("x.pdf", base64.b64encode(pdf_bytes()).decode())

    def test_timeout_is_actionable_and_temp_file_removed(self):
        paths = []
        def timeout(args, **kwargs):
            paths.append(Path(args[-1]))
            self.assertTrue(paths[-1].is_file())
            raise subprocess.TimeoutExpired(args, 30)
        with patch("power_agent.document_import.subprocess.run", side_effect=timeout):
            with self.assertRaisesRegex(ValueError, "解析超时"):
                parse_upload("x.pdf", base64.b64encode(pdf_bytes()).decode())
        self.assertFalse(paths[0].exists())

    def test_long_pages_do_not_lose_location_or_cross_page_boundary(self):
        content = "[PDF 第 1 页]\n" + "a" * 4000 + "\n\n[PDF 第 2 页]\nlast page"
        chunks = located_chunks(content, "long.pdf")
        self.assertTrue(all(c["location"] == "PDF 第 1 页" for c in chunks[:-1]))
        self.assertEqual(chunks[-1]["location"], "PDF 第 2 页")
        self.assertTrue(all(len(c["text"]) <= 900 for c in chunks))
