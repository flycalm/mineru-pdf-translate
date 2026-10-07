---
name: mineru-pdf-translate
description: Translate local PDF papers through MinerU and an OpenAI-compatible model into final PDFs. Correct OCR using source PDF crops during translation, preserve figures, and validate formula and image rendering.
---

# MinerU PDF Translate

Run `scripts/pdf_translate.py` on the folder containing the source PDFs. Translate the entire document, including appendices and captions. Deliver the final PDFs in `translated/`; keep intermediate files out of that folder. Do not produce a separate inspection report unless requested.

## Translation and correction

The default `--ocr-correction auto` supplies the translation model with original PDF crops and MinerU's OCR strings for formulas and tables. Corrections are returned separately from the translated prose, so the model cannot accidentally remove or reorder protected units.

- Correct **transcription errors against the source**, including empty-set symbols, norm bars, signs, accents, indices, equation numbering and table cell alignment. Preserve the author's mathematics, data and claims.
- Correct obvious OCR errors in prose using context. A word mistakenly detected as inline math can return to ordinary translated text.
- Use source images for units the model cannot read confidently, for invalid corrections, and for models that reject image input. Translate surrounding prose and captions normally. Internal labels in source images remain in the original language.
- Preserve formula numbering. A MathJax syntax error in a corrected formula triggers a source-image fallback during rendering.
- Source comparison needs MinerU `layout.json` and Python `PyMuPDF`. If unavailable, explain the limitation; use `--ocr-correction off` only when the user accepts translation without automatic source comparison.
- Custom `ocr_repairs.json` remains available for exact, source-verified prose repairs. Do not add paper-specific symbols, file names or inferred formulas to the built-in rules.

The correction process is conservative but cannot guarantee semantic correctness. Inspect representative complex equations, tables and the appendix ending against the original before delivery. Internal validation runs without creating a separate report.

## Configuration

Read configuration privately; never display credentials or signed storage URLs. Prefer explicit command options, then files in the PDF folder, then environment variables.

Supported files:

- `mineru密钥.txt`: MinerU token.
- `翻译大模型url以及key.txt`: base URL and API key on two lines.
- `大模型和mineru的key.txt`: labelled `model:`, `url:`, `key:` and `mineru:` fields. A value may occupy the next line. Do not duplicate these keys into the skill folder.

Environment alternatives: `MINERU_API_TOKEN`, `PDF_TRANSLATE_LLM_BASE_URL`, `PDF_TRANSLATE_LLM_API_KEY`, `PDF_TRANSLATE_MODEL`. Choose a model accepting image input for automatic OCR correction. The script uses the model supplied in configuration; the legacy default applies only when no model is specified.

The renderer needs Node.js, Playwright and an installed Edge/Chrome/Chromium browser. It discovers the Codex bundled Node/Playwright runtime when available. Outside Codex, use an installed Node `playwright` package or set `PDF_TRANSLATE_NODE_MODULES` to its `node_modules` folder. Override the executables with `PDF_TRANSLATE_NODE`, `PDF_TRANSLATE_BROWSER` or `--browser-path`. The Python `markdown` package is installed automatically if missing; `PyMuPDF` is needed for source crops.

## Commands

Resolve `<skill-dir>` to this skill's directory. On Windows, use `py` if `python` resolves to a nonworking Store alias.

Translate and retain caches for inspection or rerendering:

```powershell
python <skill-dir>\scripts\pdf_translate.py --workdir <pdf-folder> --keep-temp
```

Re-render cached translations without MinerU or model calls:

```powershell
python <skill-dir>\scripts\pdf_translate.py --workdir <pdf-folder> --render-only --keep-temp
```

Other options:

- `--workers 3`: maximum simultaneous translation requests. Lower this for a rate-limited endpoint.
- `--target-language "Japanese" --target-suffix ja`: choose another target.
- `--force`: discard document caches and rebuild. Do not use this merely to retry interrupted chunks or fix rendering.
- `--ocr-correction off`: protect extracted formulas without source comparison; use only for an explicitly accepted legacy workflow.

## Reliability and rendering

- Failed or interrupted runs retain their caches. MinerU task IDs survive upload, polling and download failures; resume an existing task rather than paying for another parse. Successful translation chunks are written atomically and reused only for matching source, model, endpoint, language and correction mode.
- Preserve source whitespace at chunk boundaries. Never concatenate stripped chunks in a way that joins headings to paragraphs or words together. Limit image evidence per request without changing source order.
- Embed local images into the render HTML and use short temporary rendering paths to avoid Windows path-loading failures. Group multi-panel figures only when the layout identifies one figure and the image references are contiguous; do not swallow captions or intervening text.
- Wait for fonts, images and MathJax startup to finish. Missing images, remaining placeholder tokens and unresolved formula errors fail rendering. Print without browser headers or footers. Replace a previous output only after a valid new PDF exists.
- Disable MathJax's `noundefined` extension so unknown commands raise detectable errors rather than printing macro names (see [MathJax v3 documentation](https://docs.mathjax.org/en/v3.2/input/tex/extensions/noundefined.html)).
- MathJax v3 is loaded from the existing CDN; if unavailable, report the render failure and retain caches. Do not treat file existence alone as proof that equations rendered.
- MinerU uploads directly to its own storage by default. Connection errors use bounded retries and can try its OSS acceleration hostname. `PDF_TRANSLATE_UPLOAD_HOST` overrides the alternate hostname. Optional `PDF_TRANSLATE_DOWNLOAD_RESOLVE=host:443:ip` uses curl's route override with normal TLS verification; do not change system DNS or disable certificate verification.
- `--keep-temp` retains MinerU output, source crops and per-chunk translation caches in `.pdf_translate_tmp/`. No QA report is written. Failed-file diagnostics stay in the temporary folder.

If the user also requests Zotero import, use an available Zotero tool, deduplicate by reliable metadata, attach the original and final translation to the same parent item, and verify the stored attachments. PDF translation alone does not request a Zotero mutation.

## Resources

- `scripts/pdf_translate.py`: batch parsing, translation, resumable caching and rendering.
- `scripts/source_evidence.py`: source-coordinate crops, formula/table pairing and correction validation.
- `scripts/render_pdf.cjs`: browser completion waits and source fallback for invalid math.
