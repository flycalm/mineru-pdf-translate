from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


MINERU_CREATE_TASK_URL = "https://mineru.net/api/v4/extract/task"
MINERU_TASK_URL_TEMPLATE = "https://mineru.net/api/v4/extract/task/{task_id}"
MINERU_BATCH_UPLOAD_URL = "https://mineru.net/api/v4/file-urls/batch"
MINERU_BATCH_RESULT_URL_TEMPLATE = "https://mineru.net/api/v4/extract-results/batch/{batch_id}"
DEFAULT_UPLOAD_API_URL = "https://tmpfiles.org/api/v1/upload"
DEFAULT_MODEL = "gpt-5.4-mini"
DEFAULT_TARGET_LANGUAGE = "Simplified Chinese"
DEFAULT_TARGET_SUFFIX = "zh"

POLL_INTERVAL_SECONDS = 10
POLL_TIMEOUT_SECONDS = 60 * 30
MAX_TRANSLATION_CHARS = 6000
LLM_MAX_RETRIES = 5


class PipelineError(RuntimeError):
    pass


@dataclass
class LlmConfig:
    base_url: str
    api_key: str
    model: str


def log(message: str) -> None:
    print(message, flush=True)


def import_markdown():
    try:
        import markdown as markdown_module  # type: ignore
    except ImportError:
        log("markdown package not found, installing it")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "markdown"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        import markdown as markdown_module  # type: ignore
    return markdown_module


def run_command(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise PipelineError(
            f"Command failed ({result.returncode}): {' '.join(args)}\n{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def run_curl(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return run_command(["curl.exe", *args], cwd=cwd)


def json_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict | None = None,
    timeout: int = 120,
) -> dict:
    data = None
    final_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        final_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=final_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise PipelineError(f"HTTP {exc.code} for {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise PipelineError(f"Request failed for {url}: {exc}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"Invalid JSON from {url}: {body[:500]}") from exc


def upload_binary(url: str, path: Path) -> None:
    run_curl(["-sS", "-X", "PUT", "-T", str(path), url])


def read_text_if_exists(path: Path) -> str | None:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return None


def read_first_existing_text(paths: Iterable[Path]) -> str | None:
    for path in paths:
        text = read_text_if_exists(path)
        if text:
            return text
    return None


def load_mineru_token(workdir: Path, token_override: str | None) -> str:
    token = token_override or read_first_existing_text(
        [
            workdir / "mineru密钥.txt",
            workdir / "mineru瀵嗛挜.txt",
        ]
    )
    if not token:
        raise PipelineError("MinerU token not found. Set MINERU_API_TOKEN or create mineru密钥.txt in the working directory.")
    return token


def load_llm_config(workdir: Path, base_url: str | None, api_key: str | None, model: str) -> LlmConfig:
    file_base_url = None
    file_api_key = None
    config_text = read_first_existing_text(
        [
            workdir / "翻译大模型url以及key.txt",
            workdir / "缈昏瘧澶фā鍨媢rl浠ュ強key.txt",
        ]
    )
    if config_text:
        lines = [line.strip() for line in config_text.splitlines() if line.strip()]
        if len(lines) >= 2:
            file_base_url = lines[0]
            file_api_key = lines[1]

    final_base_url = (base_url or file_base_url or "").rstrip("/")
    final_api_key = api_key or file_api_key or ""
    if not final_base_url or not final_api_key:
        raise PipelineError(
            "LLM config not found. Set PDF_TRANSLATE_LLM_BASE_URL and PDF_TRANSLATE_LLM_API_KEY, "
            "or create 翻译大模型url以及key.txt in the working directory."
        )
    return LlmConfig(base_url=final_base_url, api_key=final_api_key, model=model)


def detect_browser(explicit_path: str | None) -> str:
    candidates: list[str] = []
    if explicit_path:
        candidates.append(explicit_path)

    for name in ["msedge", "chrome", "google-chrome", "chromium", "chromium-browser"]:
        path = shutil.which(name)
        if path:
            candidates.append(path)

    candidates.extend(
        [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
    )

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise PipelineError(
        "No supported browser found. Set PDF_TRANSLATE_BROWSER to Edge/Chrome/Chromium executable path."
    )


def iter_pdfs(root: Path) -> Iterable[Path]:
    return sorted(
        path
        for path in root.glob("*.pdf")
        if path.is_file()
        and not path.name.startswith("_tmp_")
        and not path.name.endswith("_zh.pdf")
    )


def ensure_clean_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def upload_pdf(pdf_path: Path, upload_api_url: str) -> str:
    log(f"  Uploading {pdf_path.name} to temporary host")
    result = run_curl(["-s", "-F", f"file=@{pdf_path.name}", upload_api_url], cwd=pdf_path.parent)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"Upload response is not JSON: {result.stdout[:500]}") from exc
    if payload.get("status") != "success":
        raise PipelineError(f"Upload failed: {payload}")
    raw_url = payload["data"]["url"]
    return raw_url.replace("http://tmpfiles.org/", "https://tmpfiles.org/dl/")


def create_mineru_batch_task(pdf_path: Path, token: str) -> str:
    log(f"  Requesting MinerU upload URL for {pdf_path.name}")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "source": "codex",
    }
    payload = {
        "enable_formula": True,
        "enable_table": True,
        "language": "en",
        "files": [{"name": pdf_path.name}],
    }
    response = json_request("POST", MINERU_BATCH_UPLOAD_URL, headers=headers, payload=payload, timeout=180)
    if response.get("code") != 0:
        raise PipelineError(f"MinerU upload URL creation failed: {json.dumps(response, ensure_ascii=False)}")
    data = response.get("data", {})
    batch_id = data.get("batch_id")
    files = data.get("file_urls") or []
    upload_url = files[0].get("url") if isinstance(files[0], dict) else files[0]
    if not batch_id or not files or not upload_url:
        raise PipelineError(f"MinerU did not return upload URL: {json.dumps(response, ensure_ascii=False)}")
    log("  Uploading PDF to MinerU storage")
    upload_binary(upload_url, pdf_path)
    return batch_id


def wait_for_mineru_batch(batch_id: str, token: str) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "source": "codex",
    }
    deadline = time.time() + POLL_TIMEOUT_SECONDS
    while time.time() < deadline:
        response = json_request(
            "GET",
            MINERU_BATCH_RESULT_URL_TEMPLATE.format(batch_id=batch_id),
            headers=headers,
            timeout=120,
        )
        if response.get("code") != 0:
            raise PipelineError(f"MinerU batch polling failed: {json.dumps(response, ensure_ascii=False)}")
        files = response.get("data", {}).get("extract_result") or []
        if files:
            result = files[0]
            state = result.get("state")
            if state == "done":
                return result
            if state == "failed":
                raise PipelineError(f"MinerU parsing failed: {result.get('err_msg') or 'unknown error'}")
            log(f"  MinerU task state: {state}")
        else:
            log("  MinerU task pending")
        time.sleep(POLL_INTERVAL_SECONDS)
    raise PipelineError(f"MinerU polling timed out for batch {batch_id}")


def create_mineru_task(file_url: str, token: str) -> str:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "source": "codex",
    }
    payload = {
        "url": file_url,
        "is_ocr": False,
        "enable_formula": True,
        "enable_table": True,
        "language": "en",
    }
    response = json_request("POST", MINERU_CREATE_TASK_URL, headers=headers, payload=payload, timeout=180)
    if response.get("code") != 0:
        raise PipelineError(f"MinerU task creation failed: {json.dumps(response, ensure_ascii=False)}")
    task_id = response.get("data", {}).get("task_id")
    if not task_id:
        raise PipelineError(f"MinerU did not return task_id: {json.dumps(response, ensure_ascii=False)}")
    return task_id


def wait_for_mineru(task_id: str, token: str) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "source": "codex",
    }
    deadline = time.time() + POLL_TIMEOUT_SECONDS
    while time.time() < deadline:
        response = json_request(
            "GET",
            MINERU_TASK_URL_TEMPLATE.format(task_id=task_id),
            headers=headers,
            timeout=120,
        )
        if response.get("code") != 0:
            raise PipelineError(f"MinerU polling failed: {json.dumps(response, ensure_ascii=False)}")
        data = response.get("data", {})
        state = data.get("state")
        if state == "done":
            return data
        if state == "failed":
            raise PipelineError(f"MinerU parsing failed: {data.get('err_msg') or 'unknown error'}")
        progress = data.get("extract_progress", {})
        extracted = progress.get("extracted_pages")
        total = progress.get("total_pages")
        if extracted is not None and total is not None:
            log(f"  MinerU parsing progress: {extracted}/{total}")
        else:
            log(f"  MinerU task state: {state}")
        time.sleep(POLL_INTERVAL_SECONDS)
    raise PipelineError(f"MinerU polling timed out for task {task_id}")


def download_zip(zip_url: str, out_path: Path) -> None:
    log("  Downloading MinerU result ZIP")
    run_curl(["-L", "--http1.1", zip_url, "-o", str(out_path)])


def unzip_to(zip_path: Path, out_dir: Path) -> None:
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(out_dir)


def find_markdown_file(extract_dir: Path) -> Path:
    candidates = list(extract_dir.rglob("full.md"))
    if not candidates:
        raise PipelineError(f"No full.md found under {extract_dir}")
    return candidates[0]


def repair_common_mineru_ocr_artifacts(markdown_text: str) -> str:
    repaired = markdown_text
    replacements = {
        r"top- $\mathbf { \nabla } \cdot \mathbf { k }$": "top-k",
        r"split- $\mathbf { \nabla } \cdot \mathbf { k }$": "split-k",
        r"I C o m i p": "I C o m p",
        r"query token ??": "query token $t$",
        r"before the ??-th layer": r"before the $l$-th layer",
        r"hidden size ??.": r"hidden size $d$.",
        r"intermediate output ??′ ??′ ??′": "intermediate output",
        r"the?? intermediate output": "the intermediate output",
        r"contiguous ?? tokens": r"contiguous $s$ tokens",
        r"rank ?? sends": r"rank $i$ sends",
        r"local ?? uncompressed": r"local $s$ uncompressed",
        r"head dimension ?? to 512": r"head dimension $c$ to 512",
        r"step ??": r"step $t$",
        r"6ℎ?? FLOPs": r"$6 h d$ FLOPs",
        r"= lcm(??,??′)??′": r"$k _ { 2 } = \frac { \operatorname { l c m } ( m , m ^ { \prime } ) } { m ^ { \prime } }$",
        r"sg - log ?????? ( ???? | ??,??<?? )???? ( ???? | ??,??<?? )": r"$\mathrm { sg } \log \pi _ { \theta _ i } ( y _ t \mid x , y _ { < t } )$",
        r"{q??,??}": r"$\{ \mathbf { q } _ { t , i } \}$",
        r"CSprsComp??": r"$C _ { t } ^ { \mathsf { S p r s C o m p } }$",
    }
    for source, target in replacements.items():
        repaired = repaired.replace(source, target)
    return repaired


def protect_markdown_segments(markdown_text: str) -> tuple[str, dict[str, str]]:
    placeholders: dict[str, str] = {}

    def add_placeholder(value: str) -> str:
        key = f"@@PDF_TRANSLATE_KEEP_{len(placeholders):06d}@@"
        placeholders[key] = value
        return key

    def replace(match: re.Match[str]) -> str:
        return add_placeholder(match.group(0))

    protected = markdown_text
    protectors: list[tuple[str, int]] = [
        (r"```[\s\S]*?```", 0),
        (r"!\[[^\]]*]\([^)\n]+\)", 0),
        (r"\$\$[\s\S]*?\$\$", 0),
        (r"\\\[[\s\S]*?\\\]", 0),
        (r"\\\([\s\S]*?\\\)", 0),
        (r"(?<!\\)\$(?![\s\.,;:!?，。；：！？）\)])(?:\\.|[^$\\\n]){1,2000}(?<!\\)\$", 0),
    ]
    for pattern, flags in protectors:
        protected = re.sub(pattern, replace, protected, flags=flags)
    return protected, placeholders


def restore_placeholders(text: str, placeholders: dict[str, str]) -> str:
    restored = text
    for key, value in placeholders.items():
        restored = restored.replace(key, value)
    return restored


def protect_math_segments(text: str, prefix: str = "MATH") -> tuple[str, dict[str, str]]:
    placeholders: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        key = f"@@PDF_TRANSLATE_{prefix}_{len(placeholders):06d}@@"
        placeholders[key] = match.group(0)
        return key

    protected = text
    for pattern in [
        r"\$\$[\s\S]*?\$\$",
        r"\\\[[\s\S]*?\\\]",
        r"\\\([\s\S]*?\\\)",
        r"(?<!\\)\$(?![\s\.,;:!?，。；：！？）\)])(?:\\.|[^$\\\n]){1,2000}(?<!\\)\$",
    ]:
        protected = re.sub(pattern, replace, protected)
    return protected, placeholders


def split_long_text(text: str, limit: int = MAX_TRANSLATION_CHARS) -> list[str]:
    if len(text) <= limit:
        return [text]

    blocks = re.split(r"(\n\s*\n)", text)
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append(current)
            current = ""

    for block in blocks:
        if not block:
            continue
        if len(block) > limit:
            flush()
            lines = block.splitlines(keepends=True)
            piece = ""
            for line in lines:
                if len(piece) + len(line) > limit and piece:
                    chunks.append(piece)
                    piece = line
                else:
                    piece += line
            if piece:
                chunks.append(piece)
            continue
        if len(current) + len(block) > limit and current:
            flush()
        current += block
    flush()
    return chunks


def contains_placeholder(text: str) -> bool:
    return bool(re.search(r"@@PDF_TRANSLATE_KEEP_\d{6}@@", text))


def split_placeholder_line(line: str, limit: int) -> list[str]:
    parts = re.split(r"(@@PDF_TRANSLATE_KEEP_\d{6}@@)", line)
    pieces: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current:
            pieces.append(current)
            current = ""

    for part in parts:
        if not part:
            continue
        if contains_placeholder(part):
            if len(current) + len(part) > limit:
                flush()
            pieces.append(part)
            continue
        while part:
            remaining = limit - len(current)
            if remaining <= 0:
                flush()
                remaining = limit
            current += part[:remaining]
            part = part[remaining:]
            if len(current) >= limit:
                flush()
    flush()
    return pieces


def split_long_text_preserving_placeholders(text: str, limit: int = MAX_TRANSLATION_CHARS) -> list[str]:
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append(current)
            current = ""

    for block in re.split(r"(\n\s*\n)", text):
        if not block:
            continue
        if len(block) > limit:
            flush()
            for line in block.splitlines(keepends=True):
                line_pieces = split_placeholder_line(line, limit) if len(line) > limit else [line]
                for piece in line_pieces:
                    if len(current) + len(piece) > limit:
                        flush()
                    current += piece
            flush()
            continue
        if len(current) + len(block) > limit:
            flush()
        current += block
    flush()
    return chunks


def validate_placeholders(source: str, translated: str, chunk_index: int) -> None:
    source_placeholders = sorted(set(re.findall(r"@@PDF_TRANSLATE_KEEP_\d{6}@@", source)))
    translated_placeholders = sorted(set(re.findall(r"@@PDF_TRANSLATE_KEEP_\d{6}@@", translated)))
    if source_placeholders != translated_placeholders:
        missing = sorted(set(source_placeholders) - set(translated_placeholders))
        extra = sorted(set(translated_placeholders) - set(source_placeholders))
        raise PipelineError(
            f"Translation changed protected placeholders in chunk {chunk_index}. "
            f"Missing: {missing[:5]} Extra: {extra[:5]}"
        )


def translate_chunk(chunk: str, llm: LlmConfig, target_language: str) -> str:
    system_prompt = (
        "You are a professional translator for academic papers. "
        f"Translate the Markdown content into {target_language}. "
        "Preserve Markdown structure, heading levels, citations, numbering, tables, URLs, file paths, and placeholders exactly. "
        "Never edit tokens that match @@PDF_TRANSLATE_KEEP_000000@@ style. "
        "Before returning, verify that every placeholder from the input appears exactly once in the output. "
        "Keep technical abbreviations such as CSA, HCA, MoE, MQA, KV cache, FLOPs, FP4, FP8, BF16, top-k, rank, token, and logits stable. "
        "Translate surrounding prose naturally and consistently for an academic paper. "
        "Do not add explanations, notes, or code fences. "
        "Only output the translated Markdown."
    )
    payload = {
        "model": llm.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": chunk},
        ],
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {llm.api_key}",
        "Content-Type": "application/json",
    }
    last_error: Exception | None = None
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            response = json_request(
                "POST",
                f"{llm.base_url}/v1/chat/completions",
                headers=headers,
                payload=payload,
                timeout=300,
            )
            return response["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt == LLM_MAX_RETRIES:
                break
            delay = attempt * 5
            log(f"      Translation request failed, retrying in {delay}s: {exc}")
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def translate_markdown(markdown_path: Path, llm: LlmConfig, target_language: str) -> str:
    log(f"  Translating Markdown to {target_language}")
    source = repair_common_mineru_ocr_artifacts(markdown_path.read_text(encoding="utf-8"))
    protected, placeholders = protect_markdown_segments(source)
    chunks = split_long_text_preserving_placeholders(protected)
    translated_chunks: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        log(f"    Translating chunk {index}/{len(chunks)}")
        last_error: Exception | None = None
        for attempt in range(1, LLM_MAX_RETRIES + 1):
            translated = translate_chunk(chunk, llm, target_language)
            try:
                validate_placeholders(chunk, translated, index)
                break
            except PipelineError as exc:
                last_error = exc
                if attempt == LLM_MAX_RETRIES:
                    raise
                log(f"      Placeholder validation failed, retrying chunk {index}: {exc}")
                time.sleep(attempt * 2)
        else:
            assert last_error is not None
            raise last_error
        translated_chunks.append(translated)
    return restore_placeholders("".join(translated_chunks), placeholders)


def sanitize_html(markdown_module, markdown_text: str) -> str:
    protected, placeholders = protect_math_segments(markdown_text, prefix="HTML_MATH")
    body_html = markdown_module.markdown(
        protected,
        extensions=["tables", "fenced_code", "sane_lists", "toc", "nl2br"],
        output_format="html5",
    )
    return restore_placeholders(body_html, placeholders)


def html_template(title: str, body_html: str) -> str:
    safe_title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <script>
    window.MathJax = {{
      tex: {{
        inlineMath: [['$', '$'], ['\\\\(', '\\\\)']],
        displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']],
        processEscapes: true,
        packages: {{ '[+]': ['ams'] }}
      }},
      output: {{
        displayOverflow: 'linebreak',
        linebreaks: {{
          inline: true,
          width: '100%',
          lineleading: 0.25
        }}
      }},
      svg: {{
        fontCache: 'global'
      }},
      startup: {{
        pageReady: () => {{
          return MathJax.startup.defaultPageReady().then(() => {{
            document.body.setAttribute('data-mathjax-ready', 'true');
          }});
        }}
      }}
    }};
  </script>
  <script defer src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"></script>
  <style>
    @page {{
      size: A4;
      margin: 18mm 16mm 18mm 16mm;
    }}
    body {{
      font-family: "Microsoft YaHei", "Segoe UI", "PingFang SC", "Noto Sans CJK SC", sans-serif;
      color: #111827;
      line-height: 1.65;
      font-size: 12px;
      word-break: break-word;
    }}
    h1, h2, h3, h4, h5, h6 {{
      color: #0f172a;
      line-height: 1.3;
      margin-top: 1.2em;
      margin-bottom: 0.5em;
      page-break-after: avoid;
    }}
    h1 {{
      font-size: 22px;
      border-bottom: 1px solid #cbd5e1;
      padding-bottom: 8px;
    }}
    h2 {{ font-size: 18px; }}
    h3 {{ font-size: 15px; }}
    img {{
      max-width: 100%;
      height: auto;
      display: block;
      margin: 12px auto;
      page-break-inside: avoid;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin: 12px 0;
      font-size: 11px;
      page-break-inside: avoid;
    }}
    th, td {{
      border: 1px solid #cbd5e1;
      padding: 6px 8px;
      vertical-align: top;
    }}
    th {{
      background: #f8fafc;
    }}
    code {{
      font-family: Consolas, "Courier New", monospace;
      background: #f3f4f6;
      padding: 1px 4px;
      border-radius: 3px;
    }}
    mjx-container {{
      overflow: visible;
      max-width: 100%;
    }}
    mjx-container[display="true"] {{
      display: block;
      max-width: 100%;
      margin: 12px 0;
    }}
    mjx-container svg {{
      max-width: 100%;
      height: auto;
    }}
    pre {{
      white-space: pre-wrap;
      border: 1px solid #e5e7eb;
      background: #f8fafc;
      padding: 10px;
      overflow: hidden;
    }}
    blockquote {{
      border-left: 4px solid #cbd5e1;
      margin-left: 0;
      padding-left: 12px;
      color: #374151;
    }}
  </style>
</head>
<body>
{body_html}
</body>
</html>
"""


def render_markdown_to_pdf(
    markdown_module,
    markdown_text: str,
    work_dir: Path,
    out_pdf_path: Path,
    title: str,
    browser_path: str,
) -> None:
    body_html = sanitize_html(markdown_module, markdown_text)
    html_path = work_dir / "_render.html"
    html_path.write_text(html_template(title, body_html), encoding="utf-8")
    if out_pdf_path.exists():
        out_pdf_path.unlink()
    file_url = html_path.resolve().as_uri()
    run_command(
        [
            browser_path,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-background-networking",
            "--run-all-compositor-stages-before-draw",
            "--virtual-time-budget=15000",
            f"--print-to-pdf={out_pdf_path.resolve()}",
            "--no-pdf-header-footer",
            file_url,
        ]
    )
    if html_path.exists():
        html_path.unlink()


def process_pdf(
    pdf_path: Path,
    final_output_dir: Path,
    tmp_root: Path,
    browser_path: str,
    mineru_token: str,
    llm: LlmConfig,
    target_language: str,
    target_suffix: str,
    upload_api_url: str,
    markdown_module,
    keep_temp: bool,
) -> None:
    doc_tmp_dir = tmp_root / pdf_path.stem
    ensure_clean_dir(doc_tmp_dir)

    if upload_api_url == "mineru":
        batch_id = create_mineru_batch_task(pdf_path, mineru_token)
        log(f"  MinerU batch task created: {batch_id}")
        task_data = wait_for_mineru_batch(batch_id, mineru_token)
    else:
        upload_url = upload_pdf(pdf_path, upload_api_url)
        log("  Temporary file URL ready")

        task_id = create_mineru_task(upload_url, mineru_token)
        log(f"  MinerU task created: {task_id}")
        task_data = wait_for_mineru(task_id, mineru_token)
    zip_url = task_data.get("full_zip_url")
    if not zip_url:
        raise PipelineError(f"MinerU result missing full_zip_url: {json.dumps(task_data, ensure_ascii=False)}")

    zip_path = doc_tmp_dir / "mineru_result.zip"
    download_zip(zip_url, zip_path)

    extract_dir = doc_tmp_dir / "mineru"
    extract_dir.mkdir(parents=True, exist_ok=True)
    unzip_to(zip_path, extract_dir)

    markdown_path = find_markdown_file(extract_dir)
    translated_markdown = translate_markdown(markdown_path, llm, target_language)
    translated_markdown_path = markdown_path.parent / "translated.md"
    translated_markdown_path.write_text(translated_markdown, encoding="utf-8")

    out_pdf_path = final_output_dir / f"{pdf_path.stem}_{target_suffix}.pdf"
    render_markdown_to_pdf(
        markdown_module,
        translated_markdown,
        markdown_path.parent,
        out_pdf_path,
        pdf_path.stem,
        browser_path,
    )

    if not keep_temp:
        shutil.rmtree(doc_tmp_dir, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Translate all PDFs in a folder through MinerU and an LLM, then render final PDFs."
    )
    parser.add_argument("--workdir", default=".", help="Folder containing source PDFs and optional config files.")
    parser.add_argument("--output-dir", default="translated", help="Folder for final translated PDFs.")
    parser.add_argument("--temp-dir", default=".pdf_translate_tmp", help="Temporary working directory.")
    parser.add_argument("--target-language", default=DEFAULT_TARGET_LANGUAGE, help="Target translation language.")
    parser.add_argument("--target-suffix", default=DEFAULT_TARGET_SUFFIX, help="Suffix appended to output PDF names.")
    parser.add_argument("--mineru-token", default=None, help="Override MinerU API token.")
    parser.add_argument("--llm-base-url", default=None, help="Override OpenAI-compatible base URL.")
    parser.add_argument("--llm-api-key", default=None, help="Override OpenAI-compatible API key.")
    parser.add_argument("--llm-model", default=DEFAULT_MODEL, help="LLM model name.")
    parser.add_argument("--browser-path", default=None, help="Path to Edge/Chrome/Chromium executable.")
    parser.add_argument("--upload-api-url", default=DEFAULT_UPLOAD_API_URL, help="Temporary upload API URL.")
    parser.add_argument("--force", action="store_true", help="Rebuild PDFs even if translated outputs already exist.")
    parser.add_argument("--keep-temp", action="store_true", help="Keep temporary files after completion.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    markdown_module = import_markdown()

    workdir = Path(args.workdir).resolve()
    final_output_dir = (workdir / args.output_dir).resolve()
    tmp_root = (workdir / args.temp_dir).resolve()

    mineru_token = load_mineru_token(workdir, args.mineru_token)
    llm = load_llm_config(workdir, args.llm_base_url, args.llm_api_key, args.llm_model)
    browser_path = detect_browser(args.browser_path)

    pdfs = list(iter_pdfs(workdir))
    if not pdfs:
        log("No input PDFs found.")
        return 0

    final_output_dir.mkdir(parents=True, exist_ok=True)
    tmp_root.mkdir(parents=True, exist_ok=True)

    failures: list[dict[str, str]] = []
    for index, pdf_path in enumerate(pdfs, start=1):
        out_pdf_path = final_output_dir / f"{pdf_path.stem}_{args.target_suffix}.pdf"
        if out_pdf_path.exists() and not args.force:
            log(f"[{index}/{len(pdfs)}] Skipping existing output: {pdf_path.name}")
            continue

        log(f"[{index}/{len(pdfs)}] Processing {pdf_path.name}")
        try:
            process_pdf(
                pdf_path,
                final_output_dir,
                tmp_root,
                browser_path,
                mineru_token,
                llm,
                args.target_language,
                args.target_suffix,
                args.upload_api_url,
                markdown_module,
                args.keep_temp,
            )
            log(f"[{index}/{len(pdfs)}] Done: {pdf_path.name}")
        except Exception as exc:  # noqa: BLE001
            log(f"[{index}/{len(pdfs)}] Failed: {pdf_path.name}")
            log(f"  Error: {exc}")
            failures.append({"pdf": str(pdf_path), "error": str(exc)})

    if not args.keep_temp:
        shutil.rmtree(tmp_root, ignore_errors=True)

    failures_path = final_output_dir / "failures.json"
    if failures:
        failures_path.write_text(json.dumps({"failures": failures}, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"Completed with {len(failures)} failure(s). See {failures_path}")
        return 1

    if failures_path.exists():
        failures_path.unlink()
    log("All PDFs processed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
