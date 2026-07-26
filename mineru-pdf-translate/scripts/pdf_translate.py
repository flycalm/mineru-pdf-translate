from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

MINERU_CREATE_TASK_URL = "https://mineru.net/api/v4/extract/task"
MINERU_TASK_URL_TEMPLATE = "https://mineru.net/api/v4/extract/task/{task_id}"
MINERU_BATCH_UPLOAD_URL = "https://mineru.net/api/v4/file-urls/batch"
MINERU_BATCH_RESULT_URL_TEMPLATE = "https://mineru.net/api/v4/extract-results/batch/{batch_id}"
TMPFILES_UPLOAD_API_URL = "https://tmpfiles.org/api/v1/upload"
DEFAULT_UPLOAD_API_URL = "mineru"
DEFAULT_MODEL = "gpt-5.4-mini"
DEFAULT_TARGET_LANGUAGE = "Simplified Chinese"
DEFAULT_TARGET_SUFFIX = "zh"
USER_AGENT = "mineru-pdf-translate/1.0"

POLL_INTERVAL_SECONDS = 10
POLL_TIMEOUT_SECONDS = 60 * 30
POLL_MAX_CONSECUTIVE_ERRORS = 3
MAX_TRANSLATION_CHARS = 6000
LLM_MAX_RETRIES = 5
PLACEHOLDER_MAX_RETRIES = 3
RENDER_TIMEOUT_SECONDS = 600
# MinerU's documented per-file limit.
MAX_PDF_BYTES = 200 * 1024 * 1024

PLACEHOLDER_PATTERN = r"@@PDF_TRANSLATE_KEEP_\d{6}@@"
MATH_PATTERNS = [
    r"\$\$[\s\S]*?\$\$",
    r"\\\[[\s\S]*?\\\]",
    r"\\\([\s\S]*?\\\)",
    r"(?<!\\)\$(?![\s\.,;:!?，。；：！？）\)])(?:\\.|[^$\\\n]){1,2000}(?<!\\)\$",
]
PROTECT_PATTERNS = [
    r"```[\s\S]*?```",
    r"!\[[^\]]*]\([^)\n]+\)",
    *MATH_PATTERNS,
]

# Only structural artifacts that MinerU reproduces across documents belong here.
# Document-specific guesses go into an ocr_repairs.json file in the working directory.
BUILTIN_OCR_REPAIRS = {
    r"top- $\mathbf { \nabla } \cdot \mathbf { k }$": "top-k",
    r"split- $\mathbf { \nabla } \cdot \mathbf { k }$": "split-k",
}


class PipelineError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class LlmConfig:
    base_url: str
    api_key: str
    model: str


@dataclass
class Settings:
    workdir: Path
    tmp_root: Path
    target_language: str
    target_suffix: str
    source_language: str
    upload_api_url: str
    keep_temp: bool
    force: bool
    render_only: bool
    browser_path: str
    mineru_token: str
    llm: LlmConfig | None
    markdown_module: object
    ocr_repairs: dict[str, str]


def log(message: str) -> None:
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(message.encode("utf-8", errors="replace") + b"\n")
        sys.stdout.flush()


def import_markdown():
    try:
        import markdown as markdown_module  # type: ignore
    except ImportError:
        log("markdown package not found, installing it")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "markdown"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise PipelineError(
                "Failed to auto-install the 'markdown' package. Install it manually with "
                f"'{sys.executable} -m pip install markdown'. pip said:\n"
                f"{(result.stderr or result.stdout).strip()[-800:]}"
            )
        import importlib

        importlib.invalidate_caches()
        import markdown as markdown_module  # type: ignore
    return markdown_module


def run_command(args: list[str], cwd: Path | None = None, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise PipelineError(f"Command timed out after {timeout}s: {' '.join(args)}") from exc
    if result.returncode != 0:
        raise PipelineError(
            f"Command failed ({result.returncode}): {' '.join(args)}\n{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


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
    final_headers.setdefault("User-Agent", USER_AGENT)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        final_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=final_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise PipelineError(f"HTTP {exc.code} for {url}: {detail[:500]}", status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise PipelineError(f"Request failed for {url}: {exc}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"Invalid JSON from {url}: {body[:500]}") from exc


def upload_binary(url: str, path: Path) -> None:
    # Presigned storage URLs sign an empty Content-Type, so the request must not
    # send one; http.client is used because urllib always adds a default.
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise PipelineError(f"Unsupported upload URL: {url}")
    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(parsed.netloc, timeout=600)
    try:
        target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        with path.open("rb") as fh:
            # An explicit Content-Length keeps http.client from switching to
            # chunked encoding, which presigned storage endpoints reject.
            conn.request(
                "PUT",
                target,
                body=fh,
                headers={"Content-Length": str(path.stat().st_size)},
            )
            resp = conn.getresponse()
            body = resp.read()
        if resp.status not in (200, 201, 204):
            raise PipelineError(
                f"Upload PUT failed with HTTP {resp.status}: {body[:300].decode('utf-8', errors='replace')}",
                status=resp.status,
            )
    except (OSError, http.client.HTTPException) as exc:
        raise PipelineError(f"Upload PUT failed for {url}: {exc}") from exc
    finally:
        conn.close()


def download_zip(zip_url: str, out_path: Path) -> None:
    log("  Downloading MinerU result ZIP")
    req = urllib.request.Request(zip_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=600) as resp, out_path.open("wb") as fh:
            shutil.copyfileobj(resp, fh)
    except urllib.error.HTTPError as exc:
        raise PipelineError(f"Download failed with HTTP {exc.code} for {zip_url}", status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise PipelineError(f"Download failed for {zip_url}: {exc}") from exc


def read_text_if_exists(path: Path) -> str | None:
    if path.exists():
        # utf-8-sig strips the BOM that Windows Notepad adds; a BOM inside a
        # token silently breaks authentication otherwise.
        return path.read_text(encoding="utf-8-sig").strip()
    return None


def read_first_existing_text(paths: list[Path]) -> str | None:
    for path in paths:
        text = read_text_if_exists(path)
        if text:
            return text
    return None


def load_mineru_token(workdir: Path, token_override: str | None) -> str:
    token = token_override or read_first_existing_text(
        [
            workdir / "mineru密钥.txt",
            # Legacy mojibake name (UTF-8 bytes decoded as GBK) kept for
            # compatibility with files created by older Windows setups.
            workdir / "mineru瀵嗛挜.txt",
        ]
    )
    if not token:
        token = os.environ.get("MINERU_API_TOKEN", "").strip()
    if not token:
        raise PipelineError(
            "MinerU token not found. Set MINERU_API_TOKEN or create mineru密钥.txt in the working directory."
        )
    return token


def load_llm_config(workdir: Path, base_url: str | None, api_key: str | None, model: str) -> LlmConfig:
    file_base_url = None
    file_api_key = None
    config_text = read_first_existing_text(
        [
            workdir / "翻译大模型url以及key.txt",
            # Legacy mojibake name, see load_mineru_token.
            workdir / "缈昏瘧澶фā鍨媢rl浠ュ強key.txt",
        ]
    )
    if config_text:
        lines = [line.strip() for line in config_text.splitlines() if line.strip()]
        if len(lines) >= 2:
            file_base_url = lines[0]
            file_api_key = lines[1]

    final_base_url = (
        base_url
        or file_base_url
        or os.environ.get("PDF_TRANSLATE_LLM_BASE_URL", "").strip()
    ).rstrip("/")
    final_api_key = (
        api_key
        or file_api_key
        or os.environ.get("PDF_TRANSLATE_LLM_API_KEY", "").strip()
    )
    if not final_base_url or not final_api_key:
        raise PipelineError(
            "LLM config not found. Set PDF_TRANSLATE_LLM_BASE_URL and PDF_TRANSLATE_LLM_API_KEY, "
            "or create 翻译大模型url以及key.txt in the working directory."
        )
    return LlmConfig(base_url=final_base_url, api_key=final_api_key, model=model)


def chat_completions_url(base_url: str) -> str:
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/v1"):
        return f"{trimmed}/chat/completions"
    return f"{trimmed}/v1/chat/completions"


def detect_browser(explicit_path: str | None) -> str:
    candidates: list[str] = []
    if explicit_path:
        candidates.append(explicit_path)
    env_path = os.environ.get("PDF_TRANSLATE_BROWSER", "").strip()
    if env_path:
        candidates.append(env_path)

    for name in [
        "msedge",
        "microsoft-edge",
        "microsoft-edge-stable",
        "google-chrome",
        "google-chrome-stable",
        "chrome",
        "chromium",
        "chromium-browser",
    ]:
        path = shutil.which(name)
        if path:
            candidates.append(path)

    candidates.extend(
        [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    )

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise PipelineError(
        "No supported browser found. Pass --browser-path or set PDF_TRANSLATE_BROWSER "
        "to an Edge/Chrome/Chromium executable path."
    )


def load_ocr_repairs(workdir: Path) -> dict[str, str]:
    repairs = dict(BUILTIN_OCR_REPAIRS)
    custom_path = workdir / "ocr_repairs.json"
    text = read_text_if_exists(custom_path)
    if text:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PipelineError(f"Invalid JSON in {custom_path}: {exc}") from exc
        if not isinstance(data, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in data.items()
        ):
            raise PipelineError(f"{custom_path} must be a JSON object mapping search strings to replacements.")
        repairs.update(data)
        log(f"Loaded {len(data)} custom OCR repair rule(s) from {custom_path.name}")
    return repairs


def iter_pdfs(root: Path, target_suffix: str) -> list[Path]:
    output_marker = f"_{target_suffix}.pdf".lower()
    return sorted(
        path
        for path in root.iterdir()
        if path.is_file()
        and path.suffix.lower() == ".pdf"
        and not path.name.startswith("_tmp_")
        and not path.name.lower().endswith(output_marker)
    )


def safe_rmtree(path: Path, protected: Path) -> None:
    resolved = path.resolve()
    protected_resolved = protected.resolve()
    if resolved == protected_resolved or resolved in protected_resolved.parents:
        log(f"  Refusing to delete {resolved}: it contains the working directory")
        return
    shutil.rmtree(resolved, ignore_errors=True)


def safe_clean_dir(path: Path, protected: Path) -> None:
    if path.exists():
        safe_rmtree(path, protected)
    path.mkdir(parents=True, exist_ok=True)


def upload_pdf(pdf_path: Path, upload_api_url: str) -> str:
    log(f"  Uploading {pdf_path.name} to temporary host")
    boundary = f"----pdf-translate-{uuid.uuid4().hex}"
    filename = pdf_path.name.replace('"', "_")
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = head + pdf_path.read_bytes() + tail
    req = urllib.request.Request(
        upload_api_url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise PipelineError(f"Upload failed with HTTP {exc.code}: {detail[:300]}", status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise PipelineError(f"Upload request failed: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"Upload response is not JSON: {raw[:500]}") from exc
    if payload.get("status") != "success":
        raise PipelineError(f"Upload failed: {payload}")
    raw_url = (payload.get("data") or {}).get("url")
    if not raw_url:
        raise PipelineError(f"Upload response missing URL: {payload}")
    return re.sub(r"^https?://tmpfiles\.org/", "https://tmpfiles.org/dl/", raw_url)


def mineru_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "source": "codex",
    }


def create_mineru_batch_task(pdf_path: Path, token: str, source_language: str) -> str:
    log(f"  Requesting MinerU upload URL for {pdf_path.name}")
    payload = {
        "enable_formula": True,
        "enable_table": True,
        "language": source_language,
        "files": [{"name": pdf_path.name}],
    }
    response = json_request("POST", MINERU_BATCH_UPLOAD_URL, headers=mineru_headers(token), payload=payload, timeout=180)
    if response.get("code") != 0:
        raise PipelineError(f"MinerU upload URL creation failed: {json.dumps(response, ensure_ascii=False)}")
    data = response.get("data") or {}
    batch_id = data.get("batch_id")
    files = data.get("file_urls") or []
    upload_url = None
    if files:
        first = files[0]
        upload_url = first.get("url") if isinstance(first, dict) else first
    if not batch_id or not upload_url:
        raise PipelineError(f"MinerU did not return upload URL: {json.dumps(response, ensure_ascii=False)}")
    log("  Uploading PDF to MinerU storage")
    upload_binary(upload_url, pdf_path)
    return batch_id


def poll_mineru(fetch_url: str, token: str, describe: str, extract_state) -> dict:
    deadline = time.time() + POLL_TIMEOUT_SECONDS
    consecutive_errors = 0
    while time.time() < deadline:
        try:
            response = json_request("GET", fetch_url, headers=mineru_headers(token), timeout=120)
        except PipelineError as exc:
            consecutive_errors += 1
            if consecutive_errors >= POLL_MAX_CONSECUTIVE_ERRORS:
                raise
            log(f"  Poll request failed ({consecutive_errors}/{POLL_MAX_CONSECUTIVE_ERRORS}), retrying: {exc}")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        consecutive_errors = 0
        if response.get("code") != 0:
            raise PipelineError(f"MinerU polling failed: {json.dumps(response, ensure_ascii=False)}")
        result = extract_state(response)
        if result is not None:
            return result
        time.sleep(POLL_INTERVAL_SECONDS)
    raise PipelineError(f"MinerU polling timed out for {describe}")


def wait_for_mineru_batch(batch_id: str, token: str) -> dict:
    def extract_state(response: dict) -> dict | None:
        files = (response.get("data") or {}).get("extract_result") or []
        if not files:
            log("  MinerU task pending")
            return None
        result = files[0]
        state = result.get("state")
        if state == "done":
            return result
        if state == "failed":
            raise PipelineError(f"MinerU parsing failed: {result.get('err_msg') or 'unknown error'}")
        log(f"  MinerU task state: {state}")
        return None

    return poll_mineru(
        MINERU_BATCH_RESULT_URL_TEMPLATE.format(batch_id=batch_id),
        token,
        f"batch {batch_id}",
        extract_state,
    )


def create_mineru_task(file_url: str, token: str, source_language: str) -> str:
    payload = {
        "url": file_url,
        "is_ocr": False,
        "enable_formula": True,
        "enable_table": True,
        "language": source_language,
    }
    response = json_request("POST", MINERU_CREATE_TASK_URL, headers=mineru_headers(token), payload=payload, timeout=180)
    if response.get("code") != 0:
        raise PipelineError(f"MinerU task creation failed: {json.dumps(response, ensure_ascii=False)}")
    task_id = (response.get("data") or {}).get("task_id")
    if not task_id:
        raise PipelineError(f"MinerU did not return task_id: {json.dumps(response, ensure_ascii=False)}")
    return task_id


def wait_for_mineru(task_id: str, token: str) -> dict:
    def extract_state(response: dict) -> dict | None:
        data = response.get("data") or {}
        state = data.get("state")
        if state == "done":
            return data
        if state == "failed":
            raise PipelineError(f"MinerU parsing failed: {data.get('err_msg') or 'unknown error'}")
        progress = data.get("extract_progress") or {}
        extracted = progress.get("extracted_pages")
        total = progress.get("total_pages")
        if extracted is not None and total is not None:
            log(f"  MinerU parsing progress: {extracted}/{total}")
        else:
            log(f"  MinerU task state: {state}")
        return None

    return poll_mineru(
        MINERU_TASK_URL_TEMPLATE.format(task_id=task_id),
        token,
        f"task {task_id}",
        extract_state,
    )


def unzip_to(zip_path: Path, out_dir: Path) -> None:
    out_resolved = out_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            target = (out_dir / member.filename).resolve()
            try:
                target.relative_to(out_resolved)
            except ValueError:
                raise PipelineError(f"Unsafe path in result ZIP: {member.filename}")
        zf.extractall(out_dir)


def find_first(root: Path, name: str) -> Path | None:
    if not root.exists():
        return None
    candidates = sorted(root.rglob(name))
    return candidates[0] if candidates else None


def apply_ocr_repairs(markdown_text: str, repairs: dict[str, str]) -> str:
    repaired = markdown_text
    for source, target in repairs.items():
        repaired = repaired.replace(source, target)
    return repaired


def protect_segments(text: str, patterns: list[str], prefix: str) -> tuple[str, dict[str, str]]:
    placeholders: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        key = f"@@PDF_TRANSLATE_{prefix}_{len(placeholders):06d}@@"
        placeholders[key] = match.group(0)
        return key

    protected = text
    for pattern in patterns:
        protected = re.sub(pattern, replace, protected)
    return protected, placeholders


def restore_placeholders(text: str, placeholders: dict[str, str], transform=None) -> str:
    restored = text
    for key, value in placeholders.items():
        restored = restored.replace(key, transform(value) if transform else value)
    return restored


def split_placeholder_line(line: str, limit: int) -> list[str]:
    parts = re.split(f"({PLACEHOLDER_PATTERN})", line)
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
        if re.fullmatch(PLACEHOLDER_PATTERN, part):
            # Placeholders are atomic: never split one, but keep it in source order.
            if len(current) + len(part) > limit:
                flush()
            current += part
            continue
        while part:
            remaining = limit - len(current)
            if remaining <= 0:
                flush()
                remaining = limit
            current += part[:remaining]
            part = part[remaining:]
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
    source_counts = Counter(re.findall(PLACEHOLDER_PATTERN, source))
    translated_counts = Counter(re.findall(PLACEHOLDER_PATTERN, translated))
    if source_counts != translated_counts:
        missing = sorted(key for key in source_counts if translated_counts[key] < source_counts[key])
        extra = sorted(key for key in translated_counts if translated_counts[key] > source_counts[key])
        raise PipelineError(
            f"Translation changed protected placeholders in chunk {chunk_index}. "
            f"Missing: {missing[:5]} Extra: {extra[:5]}"
        )


def strip_wrapping_code_fence(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(r"```[a-zA-Z0-9_-]*\n([\s\S]*?)\n?```", stripped)
    # Only strip an unambiguous whole-output wrapper; leave anything containing
    # further fences untouched.
    if match and "```" not in match.group(1):
        return match.group(1)
    return text


def translate_chunk(chunk: str, llm: LlmConfig, target_language: str, placeholder_reminder: bool = False) -> str:
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
    if placeholder_reminder:
        system_prompt += (
            " CRITICAL: a previous attempt modified placeholder tokens. Copy every "
            "@@PDF_TRANSLATE_KEEP_nnnnnn@@ token exactly, character by character, without translating or reformatting it."
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
                chat_completions_url(llm.base_url),
                headers=headers,
                payload=payload,
                timeout=300,
            )
            try:
                content = response["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise PipelineError(
                    f"Unexpected LLM response shape: {json.dumps(response, ensure_ascii=False)[:500]}"
                ) from exc
            if not isinstance(content, str) or not content.strip():
                raise PipelineError("LLM returned empty content")
            return content
        except PipelineError as exc:
            # Client errors other than rate limiting will not heal on retry.
            if exc.status is not None and 400 <= exc.status < 500 and exc.status != 429:
                raise
            last_error = exc
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        if attempt == LLM_MAX_RETRIES:
            break
        delay = attempt * 5
        log(f"      Translation request failed, retrying in {delay}s: {last_error}")
        time.sleep(delay)
    assert last_error is not None
    raise last_error


def translate_markdown(markdown_path: Path, llm: LlmConfig, target_language: str, ocr_repairs: dict[str, str]) -> str:
    log(f"  Translating Markdown to {target_language}")
    source = apply_ocr_repairs(markdown_path.read_text(encoding="utf-8"), ocr_repairs)
    protected, placeholders = protect_segments(source, PROTECT_PATTERNS, "KEEP")
    chunks = split_long_text_preserving_placeholders(protected)
    translated_chunks: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        log(f"    Translating chunk {index}/{len(chunks)}")
        for attempt in range(1, PLACEHOLDER_MAX_RETRIES + 1):
            translated = strip_wrapping_code_fence(
                translate_chunk(chunk, llm, target_language, placeholder_reminder=attempt > 1)
            )
            try:
                validate_placeholders(chunk, translated, index)
                break
            except PipelineError as exc:
                if attempt == PLACEHOLDER_MAX_RETRIES:
                    raise
                log(f"      Placeholder validation failed, retrying chunk {index}: {exc}")
                time.sleep(attempt * 2)
        translated_chunks.append(translated)
    return restore_placeholders("".join(translated_chunks), placeholders)


def escape_html_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def sanitize_html(markdown_module, markdown_text: str) -> str:
    protected, placeholders = protect_segments(markdown_text, MATH_PATTERNS, "HTML_MATH")
    body_html = markdown_module.markdown(
        protected,
        extensions=["tables", "fenced_code", "sane_lists", "toc", "nl2br"],
        output_format="html5",
    )
    # Math goes back in escaped so "<", ">" and "&" inside TeX cannot be parsed
    # as HTML; the browser decodes the entities before MathJax reads the text.
    return restore_placeholders(body_html, placeholders, transform=escape_html_text)


def html_template(title: str, body_html: str, lang: str) -> str:
    safe_title = escape_html_text(title)
    safe_lang = re.sub(r"[^A-Za-z0-9-]", "", lang) or "zh"
    return f"""<!DOCTYPE html>
<html lang="{safe_lang}">
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
    lang: str,
) -> None:
    body_html = sanitize_html(markdown_module, markdown_text)
    html_path = work_dir / "_render.html"
    html_path.write_text(html_template(title, body_html, lang), encoding="utf-8")
    if out_pdf_path.exists():
        try:
            out_pdf_path.unlink()
        except OSError as exc:
            raise PipelineError(
                f"Cannot overwrite {out_pdf_path} (the file may be open in a PDF viewer): {exc}"
            ) from exc
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
        ],
        timeout=RENDER_TIMEOUT_SECONDS,
    )
    if not out_pdf_path.exists() or out_pdf_path.stat().st_size == 0:
        raise PipelineError(
            f"Browser did not produce {out_pdf_path.name}; inspect {html_path} to debug the render."
        )


def parse_pdf_with_mineru(pdf_path: Path, doc_tmp_dir: Path, extract_dir: Path, settings: Settings) -> None:
    size = pdf_path.stat().st_size
    if size > MAX_PDF_BYTES:
        raise PipelineError(f"{pdf_path.name} is {size / (1024 * 1024):.0f}MB, above MinerU's 200MB limit.")
    if settings.upload_api_url == "mineru":
        batch_id = create_mineru_batch_task(pdf_path, settings.mineru_token, settings.source_language)
        log(f"  MinerU batch task created: {batch_id}")
        task_data = wait_for_mineru_batch(batch_id, settings.mineru_token)
    else:
        file_url = upload_pdf(pdf_path, settings.upload_api_url)
        log("  Temporary file URL ready")
        task_id = create_mineru_task(file_url, settings.mineru_token, settings.source_language)
        log(f"  MinerU task created: {task_id}")
        task_data = wait_for_mineru(task_id, settings.mineru_token)
    zip_url = task_data.get("full_zip_url")
    if not zip_url:
        raise PipelineError(f"MinerU result missing full_zip_url: {json.dumps(task_data, ensure_ascii=False)}")

    zip_path = doc_tmp_dir / "mineru_result.zip"
    download_zip(zip_url, zip_path)
    safe_clean_dir(extract_dir, settings.workdir)
    unzip_to(zip_path, extract_dir)


def process_pdf(pdf_path: Path, out_pdf_path: Path, settings: Settings) -> None:
    doc_tmp_dir = settings.tmp_root / pdf_path.stem
    if settings.force and not settings.render_only:
        safe_clean_dir(doc_tmp_dir, settings.workdir)
    doc_tmp_dir.mkdir(parents=True, exist_ok=True)

    extract_dir = doc_tmp_dir / "mineru"
    markdown_path = find_first(extract_dir, "full.md")
    # The cache is per target suffix so switching --target-language never reuses
    # a translation in the wrong language. Plain translated.md is the legacy
    # name from when zh was the only target.
    translated_name = f"translated_{settings.target_suffix}.md"
    translated_path = find_first(extract_dir, translated_name)
    if translated_path is None and settings.target_suffix == DEFAULT_TARGET_SUFFIX:
        translated_path = find_first(extract_dir, "translated.md")

    if settings.render_only:
        if translated_path is None:
            raise PipelineError(
                f"No cached {translated_name} under {extract_dir} for --render-only. "
                "Run a full pass with --keep-temp first."
            )
        log(f"  Rendering from cached {translated_path.name}")
    elif markdown_path is None:
        parse_pdf_with_mineru(pdf_path, doc_tmp_dir, extract_dir, settings)
        markdown_path = find_first(extract_dir, "full.md")
        if markdown_path is None:
            raise PipelineError(f"No full.md found under {extract_dir}")
        translated_path = None
    else:
        log("  Reusing cached MinerU result")

    if translated_path is None:
        if settings.llm is None:
            raise PipelineError("LLM configuration is required to translate.")
        translated_markdown = translate_markdown(
            markdown_path, settings.llm, settings.target_language, settings.ocr_repairs
        )
        translated_path = markdown_path.parent / translated_name
        translated_path.write_text(translated_markdown, encoding="utf-8")
    else:
        if not settings.render_only:
            log(f"  Reusing cached translation ({translated_path.name})")
        translated_markdown = translated_path.read_text(encoding="utf-8")

    render_markdown_to_pdf(
        settings.markdown_module,
        translated_markdown,
        translated_path.parent,
        out_pdf_path,
        pdf_path.stem,
        settings.browser_path,
        settings.target_suffix,
    )

    if not settings.keep_temp:
        safe_rmtree(doc_tmp_dir, settings.workdir)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Translate all PDFs in a folder through MinerU and an LLM, then render final PDFs."
    )
    parser.add_argument("--workdir", default=".", help="Folder containing source PDFs and optional config files.")
    parser.add_argument("--output-dir", default="translated", help="Folder for final translated PDFs.")
    parser.add_argument("--temp-dir", default=".pdf_translate_tmp", help="Temporary working directory.")
    parser.add_argument("--target-language", default=DEFAULT_TARGET_LANGUAGE, help="Target translation language.")
    parser.add_argument("--target-suffix", default=DEFAULT_TARGET_SUFFIX, help="Suffix appended to output PDF names.")
    parser.add_argument(
        "--source-language",
        default="en",
        help="Source document language hint passed to MinerU (default: en).",
    )
    parser.add_argument("--mineru-token", default=None, help="Override MinerU API token.")
    parser.add_argument("--llm-base-url", default=None, help="Override OpenAI-compatible base URL.")
    parser.add_argument("--llm-api-key", default=None, help="Override OpenAI-compatible API key.")
    parser.add_argument(
        "--llm-model",
        default=None,
        help=f"LLM model name (default: PDF_TRANSLATE_MODEL env var or {DEFAULT_MODEL}).",
    )
    parser.add_argument("--browser-path", default=None, help="Path to Edge/Chrome/Chromium executable.")
    parser.add_argument(
        "--upload-api-url",
        default=DEFAULT_UPLOAD_API_URL,
        help=(
            '"mineru" (default) uploads directly to MinerU storage; '
            f"or a tmpfiles-compatible upload API URL such as {TMPFILES_UPLOAD_API_URL}."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Redo everything: discard cached parse/translation and rebuild existing outputs.",
    )
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="Re-render final PDFs from the cached translated Markdown without calling MinerU or the LLM.",
    )
    parser.add_argument("--keep-temp", action="store_true", help="Keep temporary files after completion.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    markdown_module = import_markdown()

    workdir = Path(args.workdir).resolve()
    if not workdir.is_dir():
        log(f"Working directory does not exist: {workdir}")
        return 2
    final_output_dir = (workdir / args.output_dir).resolve()
    tmp_root = (workdir / args.temp_dir).resolve()

    pdfs = iter_pdfs(workdir, args.target_suffix)
    if not pdfs:
        log("No input PDFs found.")
        return 0

    mineru_token = ""
    llm: LlmConfig | None = None
    if not args.render_only:
        mineru_token = load_mineru_token(workdir, args.mineru_token)
        model = args.llm_model or os.environ.get("PDF_TRANSLATE_MODEL", "").strip() or DEFAULT_MODEL
        llm = load_llm_config(workdir, args.llm_base_url, args.llm_api_key, model)
    browser_path = detect_browser(args.browser_path)
    ocr_repairs = load_ocr_repairs(workdir)

    final_output_dir.mkdir(parents=True, exist_ok=True)
    tmp_root.mkdir(parents=True, exist_ok=True)

    settings = Settings(
        workdir=workdir,
        tmp_root=tmp_root,
        target_language=args.target_language,
        target_suffix=args.target_suffix,
        source_language=args.source_language,
        upload_api_url=args.upload_api_url,
        keep_temp=args.keep_temp,
        force=args.force,
        render_only=args.render_only,
        browser_path=browser_path,
        mineru_token=mineru_token,
        llm=llm,
        markdown_module=markdown_module,
        ocr_repairs=ocr_repairs,
    )

    failures: list[dict[str, str]] = []
    for index, pdf_path in enumerate(pdfs, start=1):
        out_pdf_path = final_output_dir / f"{pdf_path.stem}_{args.target_suffix}.pdf"
        if out_pdf_path.exists() and not args.force and not args.render_only:
            log(f"[{index}/{len(pdfs)}] Skipping existing output: {pdf_path.name}")
            continue

        log(f"[{index}/{len(pdfs)}] Processing {pdf_path.name}")
        started = time.monotonic()
        try:
            process_pdf(pdf_path, out_pdf_path, settings)
            log(f"[{index}/{len(pdfs)}] Done in {time.monotonic() - started:.0f}s: {out_pdf_path.name}")
        except Exception as exc:  # noqa: BLE001
            log(f"[{index}/{len(pdfs)}] Failed: {pdf_path.name}")
            log(f"  Error: {exc}")
            failures.append({"pdf": str(pdf_path), "error": str(exc)})

    failures_path = final_output_dir / "failures.json"
    if failures:
        failures_path.write_text(json.dumps({"failures": failures}, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"Completed with {len(failures)} failure(s). See {failures_path}")
        log(f"Temporary files kept for inspection under {tmp_root}")
        return 1

    if not args.keep_temp:
        safe_rmtree(tmp_root, workdir)
    if failures_path.exists():
        failures_path.unlink()
    log("All PDFs processed successfully.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PipelineError as exc:
        log(f"Error: {exc}")
        sys.exit(2)
    except KeyboardInterrupt:
        log("Interrupted.")
        sys.exit(130)
