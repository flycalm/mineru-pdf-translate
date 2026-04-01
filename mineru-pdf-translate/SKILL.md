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
4. Run the bundled script from the target folder.
5. Deliver only the final PDFs from the `translated/` folder unless the user asks for intermediates.

## Commands

Resolve `<skill-dir>` to the `mineru-pdf-translate` skill folder on the current machine and run the bundled script from there.

Translate all PDFs in the current folder into Simplified Chinese PDFs:

```powershell
python <skill-dir>\scripts\pdf_translate.py --workdir .
```

Force rebuild existing outputs:

```powershell
python <skill-dir>\scripts\pdf_translate.py --workdir . --force
```

Translate into another language or suffix:

```powershell
python <skill-dir>\scripts\pdf_translate.py --workdir . --target-language "Japanese" --target-suffix ja
```

## Notes

- The script writes final PDFs into `translated/`.
- Temporary files go to `.pdf_translate_tmp/` and are deleted automatically unless `--keep-temp` is used.
- The script auto-installs the Python `markdown` package if it is missing.
- The script auto-detects Edge or Chrome for headless PDF printing. Override with `--browser-path` when needed.
- Long formulas are rendered with MathJax v4 and configured for automatic line breaking.
- The temporary upload step defaults to `https://tmpfiles.org/api/v1/upload`. Override with `--upload-api-url` if another compatible uploader is needed.

## Resources

### scripts/

- `scripts/pdf_translate.py`: end-to-end batch translator from local PDFs to final translated PDFs.
