---
name: mineru-pdf-translate
description: Translate local PDF papers or documents through MinerU online parsing and an OpenAI-compatible LLM, then render final translated PDFs with preserved figures and MathJax-rendered formulas. Use when the user asks to translate PDFs in a folder, especially academic papers, and wants final output PDFs rather than intermediate Markdown.
---

# MinerU PDF Translate

Use this skill when the task is "translate PDFs in this folder" and the workflow should be:
1. Parse each PDF with MinerU online API.
2. Translate the extracted Markdown with an OpenAI-compatible chat completion API.
3. Render the translated Markdown into final PDFs with images preserved and formulas rendered through MathJax.

## Workflow

1. Confirm the working directory contains the source PDFs.
2. Prefer config from the working directory:
   `mineru密钥.txt` contains the MinerU token.
   `翻译大模型url以及key.txt` contains two lines: base URL, then API key.
3. If those files are missing, use environment variables instead:
   `MINERU_API_TOKEN`
   `PDF_TRANSLATE_LLM_BASE_URL`
   `PDF_TRANSLATE_LLM_API_KEY`
   `PDF_TRANSLATE_MODEL` is optional.
4. Run the bundled script from the target folder. Direct MinerU upload is the default; add `--keep-temp` during QA so intermediates are retained, and `--force` only when a full rebuild is wanted.
5. QA the generated PDF before delivery. At minimum, check that browser headers/footers, raw LaTeX, placeholder tokens, MathJax errors, and OCR artifacts are not present.
6. Deliver only the final PDFs from the `translated/` folder unless the user asks for intermediates. If an old output PDF is locked by another app on Windows, write a clearly named optimized file such as `*_zh_optimized.pdf` instead of silently failing to overwrite it.

## Commands

Resolve `<skill-dir>` to the `mineru-pdf-translate` skill folder on the current machine and run the bundled script from there.

Translate all PDFs in the current folder into Simplified Chinese PDFs:

```powershell
python <skill-dir>\scripts\pdf_translate.py --workdir .
```

Keep intermediates for QA:

```powershell
python <skill-dir>\scripts\pdf_translate.py --workdir . --keep-temp
```

Force a full rebuild (discards cached parse and translation):

```powershell
python <skill-dir>\scripts\pdf_translate.py --workdir . --force
```

Re-render final PDFs from cached translations after HTML/CSS-only changes:

```powershell
python <skill-dir>\scripts\pdf_translate.py --workdir . --render-only --keep-temp
```

Translate into another language or suffix:

```powershell
python <skill-dir>\scripts\pdf_translate.py --workdir . --target-language "Japanese" --target-suffix ja
```

On Windows, if `python` resolves to the Microsoft Store app alias and fails, use `py` instead:

```powershell
py <skill-dir>\scripts\pdf_translate.py --workdir . --keep-temp
```

## QA And Repair

The normal workflow should translate the full PDF directly. Do not create key-page samples unless actively debugging the skill implementation.

The script is expected to protect formulas, images, and code before LLM translation; render formulas through MathJax v3 from the jsdelivr CDN; suppress Chrome/Edge PDF headers and footers; retain `translated_<suffix>.md` with `--keep-temp`; and apply built-in plus `ocr_repairs.json` OCR repairs. After a run, inspect the final PDF and text QA output.

Useful Poppler commands:

```powershell
pdftoppm -r 120 -f 1 -l 1 .\translated\paper_zh.pdf .\qa_pages\p01 -png
pdftotext -layout -enc UTF-8 .\translated\paper_zh.pdf -
pdfinfo .\translated\paper_zh.pdf
```

Recommended text QA checks on the final PDF:

```powershell
$txt = pdftotext -layout -enc UTF-8 '.\translated\paper_zh.pdf' -
[pscustomobject]@{
  FooterUri        = ($txt | Select-String -SimpleMatch 'file:///' | Measure-Object).Count
  KeepPlaceholders = ($txt | Select-String -SimpleMatch '@@PDF_TRANSLATE_KEEP_' | Measure-Object).Count
  MathError        = ($txt | Select-String -Pattern 'Missing|unrecognized|delimiter|MathJax' | Measure-Object).Count
  QuestionPairs    = ([regex]::Matches(($txt -join "`n"), '\?\?')).Count
  RawLatex         = ($txt | Select-String -Pattern '\\begin\{|\\mathbb|\\frac|\$\$' | Measure-Object).Count
  TopNabla         = ($txt | Select-String -SimpleMatch 'nabla' | Measure-Object).Count
  IComip           = ($txt | Select-String -SimpleMatch 'IComip' | Measure-Object).Count
} | Format-List
```

For a clean final render, these counts should normally be zero. Some nonzero results may be acceptable only after comparing against the original PDF and confirming they are legitimate content rather than OCR/rendering artifacts.

If only HTML/CSS/rendering rules changed, rerun with `--render-only --keep-temp` to re-render from the cached `translated_<suffix>.md` without calling MinerU or the LLM. If translation quality or placeholder preservation changed, rerun with `--force`.

## Notes

- The script writes final PDFs into `translated/`.
- Temporary files go to `.pdf_translate_tmp/` and are deleted automatically unless `--keep-temp` is used or a document fails. With `--keep-temp`, the script keeps `full.md`, extracted images, and `translated_<suffix>.md`.
- Interrupted or failed runs resume automatically: cached `full.md` skips MinerU, cached `translated_<suffix>.md` skips the LLM. Use `--force` to discard the cache.
- The script uses only the Python standard library for networking; `curl` is not required.
- The script auto-installs the Python `markdown` package if it is missing.
- The script auto-detects Edge or Chrome for headless PDF printing. Override with `--browser-path` or the `PDF_TRANSLATE_BROWSER` environment variable.
- Math formulas are protected before LLM translation so the model should not edit variables, delimiters, or image links.
- Math formulas are rendered with MathJax v3. Do not switch to MathJax v4 CDN unless the exact URL has been verified; an unavailable MathJax script causes raw LaTeX to print into the PDF.
- Use `--no-pdf-header-footer` for Chrome/Edge PDF printing. Older `--print-to-pdf-no-header` may not work and can leave every page polluted with dates, titles, `file:///.../_render.html`, and page numbers.
- If the output PDF is open in a viewer on Windows, the file may be locked; the script reports this instead of failing silently.
- Upload defaults to MinerU's direct upload flow (`--upload-api-url mineru`), so source PDFs are not sent to a third-party temporary host. Pass a tmpfiles-compatible URL such as `https://tmpfiles.org/api/v1/upload` to use one instead.
- Common MinerU OCR artifacts in technical PDFs include `top- $\mathbf { \nabla } \cdot \mathbf { k }$` for `top-k` and `??` in place of variables. Built-in repairs cover only cross-document artifacts; add document-specific rules to an `ocr_repairs.json` file (a JSON object mapping exact source strings to replacements) in the working directory, and compare against the source PDF when uncertain.

## Resources

### scripts/

- `scripts/pdf_translate.py`: end-to-end batch translator from local PDFs to final translated PDFs.
