import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mineru-pdf-translate/scripts"))
import pdf_translate as pipeline
from source_evidence import safe_table


class PipelineRegressionTests(unittest.TestCase):
    def test_reordered_formulas_are_rejected(self):
        with self.assertRaises(pipeline.PipelineError):
            pipeline.validate_placeholders("@@PDF_TRANSLATE_KEEP_000001@@ @@PDF_TRANSLATE_KEEP_000002@@",
                                           "@@PDF_TRANSLATE_KEEP_000002@@ @@PDF_TRANSLATE_KEEP_000001@@", 1)

    def test_chunk_boundaries_resume_and_model_cache_invalidation(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "full.md"
            text = "\n\n".join(f"## Section {i}\n\n" + ("paragraph word " * 210) for i in range(5))
            source.write_text(text, encoding="utf-8")
            config = pipeline.LlmConfig("http://localhost", "fake", "model-a")
            calls = []

            def interrupted(chunk, *args, **kwargs):
                calls.append(chunk)
                if len(calls) == 2:
                    raise pipeline.PipelineError("temporary service failure", status=401)
                return chunk.strip()

            with patch.object(pipeline, "translate_chunk", side_effect=interrupted):
                with self.assertRaises(pipeline.PipelineError):
                    pipeline.translate_markdown(source, config, "Chinese", {}, correction="off", workers=1)
            completed = calls[0]
            calls.clear()
            with patch.object(pipeline, "translate_chunk", side_effect=lambda c, *a, **k: calls.append(c) or c.strip()):
                output = pipeline.translate_markdown(source, config, "Chinese", {}, correction="off", workers=1)
                self.assertEqual(output, text)
                self.assertNotIn(completed, calls)
                calls.clear()
                config.model = "model-b"
                pipeline.translate_markdown(source, config, "Chinese", {}, correction="off", workers=1)
                self.assertIn(completed, calls)

    def test_uncertain_and_invalid_corrections_use_original(self):
        entry = {"id": "@@PDF_TRANSLATE_KEEP_000001@@", "kind": "display",
                 "original": r"$$x=0\tag{2}$$", "fallback": '<img src="original.png">'}
        result = {"translation": entry["id"], "corrections": [
            {"id": entry["id"], "status": "confirmed", "value": r"$$x=0\tag{3}$$"}]}
        _, replacements = pipeline.resolve_corrections(result, [entry])
        self.assertEqual(replacements[entry["id"]], entry["fallback"])
        result["corrections"][0]["status"] = "uncertain"
        _, replacements = pipeline.resolve_corrections(result, [entry])
        self.assertEqual(replacements[entry["id"]], entry["fallback"])

    def test_confirmed_source_formula_is_accepted_with_render_fallback(self):
        entry = {"id": "@@PDF_TRANSLATE_KEEP_000001@@", "kind": "inline",
                 "original": r"$\mathcal{O}$", "fallback": '<img src="empty-set.png">'}
        result = {"translation": entry["id"], "corrections": [
            {"id": entry["id"], "status": "confirmed", "value": r"$\emptyset$"}]}
        _, replacements = pipeline.resolve_corrections(result, [entry])
        self.assertIn(r"$\emptyset$", replacements[entry["id"]])
        self.assertIn("data-source-fallback", replacements[entry["id"]])

    def test_false_math_word_can_return_to_prose(self):
        entry = {"id": "@@PDF_TRANSLATE_KEEP_000001@@", "kind": "inline", "original": "$At$", "fallback": "crop"}
        result = {"translation": entry["id"], "corrections": [
            {"id": entry["id"], "status": "prose", "value": "在"}]}
        _, replacements = pipeline.resolve_corrections(result, [entry])
        self.assertEqual(replacements[entry["id"]], "在")

    def test_corrected_tables_reject_active_or_unbalanced_html(self):
        self.assertEqual(safe_table('<table><tr><td>3.14</td></tr></table>'), '<table><tr><td>3.14</td></tr></table>')
        for bad in ['<table><tr><td>3.14</tr></table>', '<table><tr><td onclick="x()">3</td></tr></table>',
                    '<table><script>evil()</script></table>']:
            with self.assertRaises(ValueError):
                safe_table(bad)

    def test_confirmed_table_keeps_source_fallback_for_cell_math(self):
        entry = {"id": "@@PDF_TRANSLATE_KEEP_000001@@", "kind": "table", "original": "OCR table",
                 "fallback": '<img src="source-table.png">'}
        result = {"translation": entry["id"], "corrections": [
            {"id": entry["id"], "status": "confirmed", "value": '<table><tr><td>$x$</td></tr></table>'}]}
        _, replacements = pipeline.resolve_corrections(result, [entry])
        self.assertIn("data-source-fallback", replacements[entry["id"]])
        self.assertIn('<table><tr><td>$x$</td></tr></table>', replacements[entry["id"]])

    def test_config_and_signed_url_redaction(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "大模型和mineru的key.txt").write_text("model: vision-model\nurl: http://localhost:1\nkey: private-llm-key\nmineru:\nprivate-mineru-key", encoding="utf-8-sig")
            self.assertEqual(pipeline.load_mineru_token(root, None), "private-mineru-key")
            self.assertEqual(pipeline.load_llm_config(root, None, None, None).model, "vision-model")
            redacted = pipeline.redact("private-llm-key https://storage/path?signature=private-mineru-key")
            self.assertNotIn("private-llm-key", redacted)
            self.assertNotIn("signature", redacted)
            self.assertEqual(pipeline.chat_completions_url("http://host/v1/chat/completions"), "http://host/v1/chat/completions")

    def test_upload_failure_resumes_batch_without_another_task(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pdf = root / "source.pdf"
            pdf.write_bytes(b"fake source PDF")
            tmp = root / "tmp"
            tmp.mkdir()
            settings = pipeline.Settings(root, tmp, "Chinese", "zh", "en", "mineru", True, False, False,
                                         "browser", "fake-token", None, None, {})
            response = {"code": 0, "data": {"batch_id": "batch-1", "file_urls": ["https://storage/upload?signature=secret"]}}

            def download(url, target):
                with zipfile.ZipFile(target, "w") as archive:
                    archive.writestr("full.md", "Parsed source")

            with patch.object(pipeline, "json_request", return_value=response) as create, \
                 patch.object(pipeline, "upload_binary", side_effect=[pipeline.PipelineError("connection lost"), None]), \
                 patch.object(pipeline, "wait_for_mineru_batch", return_value={"full_zip_url": "https://storage/result"}), \
                 patch.object(pipeline, "download_zip", side_effect=download):
                with self.assertRaises(pipeline.PipelineError):
                    pipeline.parse_pdf_with_mineru(pdf, tmp, tmp / "mineru", settings)
                pipeline.parse_pdf_with_mineru(pdf, tmp, tmp / "mineru", settings)
                self.assertEqual(create.call_count, 1)
                self.assertEqual((tmp / "mineru/full.md").read_text(), "Parsed source")
                state = json.loads((tmp / "mineru_task.json").read_text())
                self.assertTrue(state["uploaded"])
                self.assertNotIn("upload_url", state)

    def test_embedded_fallback_images_do_not_depend_on_long_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "original.png").write_bytes(b"image-bytes")
            markup = '<span data-source-fallback="&lt;img src=&quot;original.png&quot;&gt;">$x$</span>'
            embedded = pipeline.embed_local_images(markup, root)
            self.assertIn("data:image/png;base64,", embedded)
            with self.assertRaises(pipeline.PipelineError):
                pipeline.embed_local_images('<img src="missing.png">', root)


if __name__ == "__main__":
    unittest.main()
