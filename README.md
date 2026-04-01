# MinerU PDF Translate

`mineru-pdf-translate` is a Codex skill plus a standalone Python script for translating local PDF documents through:

1. MinerU online parsing
2. An OpenAI-compatible chat completion API
3. Browser-based PDF rendering with preserved images and MathJax-rendered formulas

It is designed for paper and document translation workflows where the final deliverable should be a translated PDF, not just intermediate Markdown.

## What It Does

- Parses each local PDF with the MinerU online API
- Downloads the extracted Markdown package from MinerU
- Translates Markdown through an OpenAI-compatible API
- Preserves image links during translation
- Renders the translated Markdown back into a polished PDF
- Supports batch processing for all PDFs in a folder

## Repository Layout

```text
.
|-- SKILL.md
|-- README.md
|-- agents/
|   `-- openai.yaml
`-- scripts/
    `-- pdf_translate.py
```

## Requirements

- Python 3.10+
- `curl.exe` available on the system
- Microsoft Edge, Google Chrome, or Chromium installed for headless PDF printing
- A MinerU API token
- An OpenAI-compatible API endpoint and key

The script auto-installs the `markdown` Python package if it is missing.

## Quick Start

### Option 1: Use as a Codex skill

Place this repository under your Codex skills directory, for example:

```powershell
Copy-Item -Recurse . $HOME\.codex\skills\mineru-pdf-translate
```

Then invoke the skill in Codex for a folder containing PDFs.

### Option 2: Run the script directly

From a folder that contains your source PDFs:

```powershell
python <skill-dir>\scripts\pdf_translate.py --workdir .
```

Example:

```powershell
python C:\path\to\mineru-pdf-translate\scripts\pdf_translate.py --workdir .
```

## Configuration

The script prefers local config files in the working directory. If those are missing, it falls back to environment variables.

### Local config files

Create these files in the same folder as the PDFs:

1. `mineru密钥.txt`
   Content: your MinerU API token
2. `翻译大模型url以及key.txt`
   Line 1: OpenAI-compatible base URL
   Line 2: API key

### Environment variables

You can use environment variables instead:

```powershell
$env:MINERU_API_TOKEN="your_mineru_token"
$env:PDF_TRANSLATE_LLM_BASE_URL="https://your-api-base-url"
$env:PDF_TRANSLATE_LLM_API_KEY="your_api_key"
$env:PDF_TRANSLATE_MODEL="gpt-5.4-mini"
```

`PDF_TRANSLATE_MODEL` is optional. The default model is `gpt-5.4-mini`.

## Usage

Translate all PDFs in the current folder into Simplified Chinese:

```powershell
python <skill-dir>\scripts\pdf_translate.py --workdir .
```

Force rebuild existing outputs:

```powershell
python <skill-dir>\scripts\pdf_translate.py --workdir . --force
```

Translate into another language and use a custom filename suffix:

```powershell
python <skill-dir>\scripts\pdf_translate.py --workdir . --target-language "Japanese" --target-suffix ja
```

Specify API settings directly from the command line:

```powershell
python <skill-dir>\scripts\pdf_translate.py `
  --workdir . `
  --mineru-token "your_mineru_token" `
  --llm-base-url "https://your-api-base-url" `
  --llm-api-key "your_api_key" `
  --llm-model "gpt-5.4-mini"
```

## Main Arguments

- `--workdir`: folder containing PDFs and optional config files
- `--output-dir`: folder for final translated PDFs, default `translated`
- `--temp-dir`: temporary working directory, default `.pdf_translate_tmp`
- `--target-language`: translation target language, default `Simplified Chinese`
- `--target-suffix`: suffix added to output filenames, default `zh`
- `--mineru-token`: override MinerU API token
- `--llm-base-url`: override OpenAI-compatible base URL
- `--llm-api-key`: override OpenAI-compatible API key
- `--llm-model`: override model name
- `--browser-path`: explicit path to Edge/Chrome/Chromium
- `--upload-api-url`: override the temporary upload API
- `--force`: rebuild outputs even if they already exist
- `--keep-temp`: keep temporary files after completion

## Output

- Final PDFs are written to `translated/`
- Temporary files are written to `.pdf_translate_tmp/`
- When failures occur, a `translated/failures.json` file is generated

The generated PDF filenames follow this pattern:

```text
original_name_<target-suffix>.pdf
```

Example:

```text
paper_zh.pdf
```

## Workflow Summary

For each PDF in the target folder, the script:

1. Uploads the PDF to a temporary file hosting service
2. Creates a MinerU extraction task
3. Polls MinerU until extraction is complete
4. Downloads the MinerU ZIP result
5. Finds `full.md` in the extracted package
6. Protects image references and translates Markdown in chunks
7. Renders translated Markdown to HTML
8. Prints the HTML to a final PDF with a headless browser

## Notes

- The default temporary upload API is `https://tmpfiles.org/api/v1/upload`
- You can override the upload endpoint with `--upload-api-url`
- The script only scans top-level `*.pdf` files in the specified working directory
- Existing `_zh.pdf` outputs are ignored as source inputs
- Long formulas are rendered with MathJax v4 and automatic line breaking

## Intended Use

This repository is primarily intended for:

- Translating academic papers into Chinese or other languages
- Batch translation of local PDF documents
- Codex skill workflows that should return final translated PDFs instead of intermediate Markdown

## License

No license file is included yet. Add one before wider public reuse if you want to grant explicit reuse permissions.
