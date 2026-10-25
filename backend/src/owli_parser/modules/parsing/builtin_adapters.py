"""内置 LocalAdapter 实现：text / pdf-text / office。

- 优先直接包装成熟引擎：pypdf、python-docx、python-pptx、openpyxl。
- 未安装对应依赖时 supports() 返回不支持并说明原因（不伪装可用）。
- 每种 adapter 记录 engine 与 version，规范化层据此写入 provenance。
"""

from __future__ import annotations

import csv
import io
import json
import posixpath
import re
import zipfile
from email import policy as email_policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote

from defusedxml.ElementTree import ParseError
from defusedxml.ElementTree import fromstring as safe_fromstring

from owli_parser.modules.parsing.adapters_base import (
    ExecutionContext,
    ExtractedBlock,
    ExtractedPage,
    ParseFailedError,
    ParseOutcome,
    ParserAdapter,
    ProbeFeatures,
    SourceHandle,
    SupportDecision,
)

_TEXT_EXTENSIONS = {"txt", "md", "markdown", "csv", "log", "rst", "tex", "json", "yaml", "yml"}
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_TEXT_CHUNK_CHARS = 2000  # 单块正文上限（正式 token 预算在分块阶段控制）

# ZIP：只抽取文本类条目；二进制条目记 warning，嵌套 ZIP 受深度/压缩比/条目数限制
_ARCHIVE_TEXT_EXTENSIONS = {
    "txt",
    "md",
    "markdown",
    "csv",
    "json",
    "yaml",
    "yml",
    "log",
    "rst",
    "tex",
    "xml",
    "html",
    "htm",
    "xhtml",
}
_ARCHIVE_MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024


def _split_long_text(text: str) -> list[str]:
    if len(text) <= _TEXT_CHUNK_CHARS:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > _TEXT_CHUNK_CHARS:
        cut = remaining.rfind("\n", 0, _TEXT_CHUNK_CHARS)
        if cut <= 0:
            cut = _TEXT_CHUNK_CHARS
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        parts.append(remaining)
    return parts


class TextLocalAdapter:
    """TXT / MD / CSV 等文本格式：结构优先（标题、段落、列表、代码）。"""

    name = "text-local"
    version = "0.1.0"
    mode = "local"

    def supports(self, features: ProbeFeatures) -> SupportDecision:
        if features.extension in _TEXT_EXTENSIONS or (features.mime and features.mime.startswith("text/")):
            return SupportDecision(supported=True)
        if features.mime == "text/plain":
            return SupportDecision(supported=True)
        return SupportDecision(supported=False, reason="非文本格式")

    def capabilities(self) -> dict[str, object]:
        return {"formats": sorted(_TEXT_EXTENSIONS), "unit": "document"}

    def parse(
        self, handle: SourceHandle, options: dict[str, object], execution: ExecutionContext
    ) -> ParseOutcome:
        del options
        execution.check_cancelled()
        text = _read_text(handle.path)
        if handle.path.suffix.lower() == ".csv":
            return _parse_csv(handle, text)

        outcome = ParseOutcome(
            blocks=[], unit_kind="document", engine="builtin-text", engine_version=self.version
        )
        heading_path: list[str] = []
        order = 0
        md = handle.path.suffix.lower() in {".md", ".markdown"}
        for para in _split_paragraphs(text):
            execution.check_cancelled()
            block_type = "text"
            payload: dict[str, object] = {"heading_path": list(heading_path)}
            if md:
                match = _MD_HEADING_RE.match(para)
                if match:
                    level = len(match.group(1))
                    title = match.group(2).strip()
                    heading_path = [*heading_path[: level - 1], title]
                    block_type = "heading"
                    payload = {"heading_path": list(heading_path), "level": level}
                    para = title
            elif para.startswith(("- ", "* ", "+ ")):
                block_type = "list"
                payload = {"heading_path": list(heading_path), "list_ordered": False}
            elif para.startswith("```"):
                block_type = "code"
                payload = {"heading_path": list(heading_path)}
            for piece in _split_long_text(para):
                if not piece.strip():
                    continue
                outcome.blocks.append(
                    ExtractedBlock(
                        type=block_type,
                        text=piece,
                        source_ref={"kind": "text_span", "start": 0, "end": len(piece)},
                        payload=payload,
                    )
                )
                order += 1
        return outcome


class ZipLocalAdapter:
    """ZIP 安全解包：只抽取文本类条目（支持嵌套），其余条目记 warning。

    - 限制：条目数/嵌套深度/压缩比/解压总量（防 zip bomb）；不安全路径与加密条目记
      unit_error（显式缺失，不静默跳过）；
    - 来源锚点：text_span + part="zip:<entry path>"（块内偏移，不伪装原件精确偏移）；
    - 无可解析文本条目 → archive_no_text_entries 失败（不静默产出空文档）。
    """

    name = "zip-local"
    version = "0.1.0"
    mode = "local"

    def supports(self, features: ProbeFeatures) -> SupportDecision:
        if features.extension == "zip" or features.mime == "application/zip":
            return SupportDecision(supported=True)
        return SupportDecision(supported=False, reason="非 ZIP")

    def capabilities(self) -> dict[str, object]:
        return {"formats": ["zip"], "unit": "document", "nested": True}

    def parse(
        self, handle: SourceHandle, options: dict[str, object], execution: ExecutionContext
    ) -> ParseOutcome:
        execution.check_cancelled()
        max_entries = _archive_option(options, "archive_max_entries", 1000)
        max_depth = _archive_option(options, "archive_max_depth", 3)
        max_ratio = _archive_option(options, "archive_max_ratio", 100)
        outcome = ParseOutcome(
            blocks=[], unit_kind="document", engine="python-zipfile", engine_version=self.version
        )
        state = {"entries": 0, "units": 0, "succeeded": 0, "uncompressed": 0}
        try:
            with zipfile.ZipFile(handle.path) as archive:
                self._walk(
                    archive,
                    prefix="",
                    depth=0,
                    max_depth=max_depth,
                    max_ratio=max_ratio,
                    max_entries=max_entries,
                    state=state,
                    outcome=outcome,
                    execution=execution,
                )
        except zipfile.BadZipFile:
            outcome.total_units = 1
            outcome.succeeded_units = 0
            outcome.add_unit_error("document", "invalid_zip", "ZIP 无法打开", retryable=False)
            return outcome
        if state["succeeded"] == 0 and not outcome.unit_errors:
            raise ParseFailedError("archive_no_text_entries", "压缩包内没有可解析的文本条目", retryable=False)
        outcome.total_units = max(state["units"], 1)
        outcome.succeeded_units = state["succeeded"]
        return outcome

    def _walk(
        self,
        archive: zipfile.ZipFile,
        *,
        prefix: str,
        depth: int,
        max_depth: int,
        max_ratio: int,
        max_entries: int,
        state: dict[str, int],
        outcome: ParseOutcome,
        execution: ExecutionContext,
    ) -> None:
        for info in archive.infolist():
            execution.check_cancelled()
            if info.is_dir():
                continue
            name = f"{prefix}{info.filename}"
            state["entries"] += 1
            if state["entries"] > max_entries:
                outcome.add_unit_error(
                    name,
                    "archive_too_many_entries",
                    f"条目数超过上限 {max_entries}",
                    retryable=False,
                )
                return
            if _unsafe_archive_name(info.filename):
                state["units"] += 1
                outcome.add_unit_error(name, "archive_unsafe_path", "条目路径不安全，已跳过", retryable=False)
                continue
            if info.flag_bits & 0x1:
                state["units"] += 1
                outcome.add_unit_error(name, "archive_encrypted_entry", "加密条目无法解析", retryable=False)
                continue
            if info.file_size > max(info.compress_size, 1) * max_ratio:
                state["units"] += 1
                outcome.add_unit_error(
                    name, "archive_ratio_exceeded", "单条目压缩比超过安全上限", retryable=False
                )
                continue
            state["uncompressed"] += info.file_size
            if state["uncompressed"] > _ARCHIVE_MAX_UNCOMPRESSED_BYTES:
                state["units"] += 1
                outcome.add_unit_error(
                    name, "archive_uncompressed_too_large", "解压总量超过安全上限", retryable=False
                )
                return
            extension = Path(info.filename).suffix.lower().lstrip(".")
            if extension == "zip":
                if depth + 1 > max_depth:
                    outcome.warnings.append(f"嵌套压缩包超过深度上限，已跳过：{name}")
                    continue
                try:
                    nested_bytes = archive.read(info)
                    with zipfile.ZipFile(io.BytesIO(nested_bytes)) as nested:
                        self._walk(
                            nested,
                            prefix=f"{name}!/",
                            depth=depth + 1,
                            max_depth=max_depth,
                            max_ratio=max_ratio,
                            max_entries=max_entries,
                            state=state,
                            outcome=outcome,
                            execution=execution,
                        )
                except zipfile.BadZipFile:
                    state["units"] += 1
                    outcome.add_unit_error(name, "invalid_nested_zip", "嵌套 ZIP 无法打开", retryable=False)
                continue
            if extension not in _ARCHIVE_TEXT_EXTENSIONS:
                outcome.warnings.append(f"跳过非文本条目：{name}")
                continue
            text = _decode_archive_text(archive.read(info))
            if not text.strip():
                outcome.warnings.append(f"跳过空文本条目：{name}")
                continue
            state["units"] += 1
            state["succeeded"] += 1
            for piece in _split_long_text(text):
                outcome.blocks.append(
                    ExtractedBlock(
                        type="text",
                        text=piece,
                        source_ref={
                            "kind": "text_span",
                            "part": f"zip:{name}",
                            "path": name,
                            "start": 0,
                            "end": len(piece),
                        },
                        payload={"heading_path": []},
                    )
                )


def _archive_option(options: dict[str, object], key: str, default: int) -> int:
    value = options.get(key)
    return default if value is None else int(cast(Any, value))


def _decode_archive_text(data: bytes) -> str:
    if b"\x00" in data[:4096]:
        return ""
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def _unsafe_archive_name(name: str) -> bool:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        return True
    return any(part == ".." for part in normalized.split("/"))


class JupyterLocalAdapter:
    """Jupyter Notebook（.ipynb）：按单元格顺序抽取 markdown/code/raw 文本。

    - markdown 单行标题 → heading（进入 heading_path）；代码块带 code_language；
    - 输出/图片不纳入正文（避免 base64 噪声）；无有效单元格 → empty_notebook 失败；
    - 来源锚点：text_span + part="cells[i]"（块内偏移）。
    """

    name = "jupyter-local"
    version = "0.1.0"
    mode = "local"

    def supports(self, features: ProbeFeatures) -> SupportDecision:
        if features.extension == "ipynb" or features.mime == "application/x-ipynb+json":
            return SupportDecision(supported=True)
        return SupportDecision(supported=False, reason="非 Jupyter Notebook")

    def capabilities(self) -> dict[str, object]:
        return {"formats": ["ipynb"], "unit": "document"}

    def parse(
        self, handle: SourceHandle, options: dict[str, object], execution: ExecutionContext
    ) -> ParseOutcome:
        del options
        execution.check_cancelled()
        outcome = ParseOutcome(
            blocks=[], unit_kind="document", engine="jupyter-json", engine_version=self.version
        )
        try:
            payload = json.loads(handle.path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            outcome.total_units = 1
            outcome.succeeded_units = 0
            outcome.add_unit_error("document", "invalid_notebook", "Notebook JSON 无法解析", retryable=False)
            return outcome
        cells = payload.get("cells") if isinstance(payload, dict) else None
        if not isinstance(cells, list):
            outcome.total_units = 1
            outcome.succeeded_units = 0
            outcome.add_unit_error(
                "document", "invalid_notebook", "Notebook 缺少 cells 数组", retryable=False
            )
            return outcome

        language = _notebook_language(payload)
        heading_path: list[str] = []
        for index, cell in enumerate(cells):
            execution.check_cancelled()
            if not isinstance(cell, dict):
                continue
            source = cell.get("source")
            text = (
                "".join(str(part) for part in source)
                if isinstance(source, list)
                else source
                if isinstance(source, str)
                else ""
            ).strip()
            if not text:
                continue
            cell_type = cell.get("cell_type")
            if cell_type == "markdown":
                match = _MD_HEADING_RE.match(text)
                if match and "\n" not in text:
                    level = len(match.group(1))
                    title = match.group(2).strip()
                    heading_path = [*heading_path[: level - 1], title]
                    block_type = "heading"
                    text = title
                    block_payload: dict[str, object] = {
                        "heading_path": list(heading_path),
                        "level": level,
                    }
                else:
                    block_type = "text"
                    block_payload = {"heading_path": list(heading_path)}
            elif cell_type == "code":
                block_type = "code"
                block_payload = {"heading_path": list(heading_path), "code_language": language}
            else:
                block_type = "text"
                block_payload = {"heading_path": list(heading_path)}
            for piece in _split_long_text(text):
                outcome.blocks.append(
                    ExtractedBlock(
                        type=block_type,
                        text=piece,
                        source_ref={
                            "kind": "text_span",
                            "part": f"cells[{index}]",
                            "start": 0,
                            "end": len(piece),
                        },
                        payload=block_payload,
                    )
                )
        if not outcome.blocks:
            raise ParseFailedError("empty_notebook", "Notebook 没有可解析的单元格", retryable=False)
        outcome.total_units = 1
        outcome.succeeded_units = 1
        return outcome


def _notebook_language(payload: dict[str, object]) -> str | None:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None
    kernelspec = metadata.get("kernelspec")
    if isinstance(kernelspec, dict) and isinstance(kernelspec.get("language"), str):
        return str(kernelspec["language"])
    language_info = metadata.get("language_info")
    if isinstance(language_info, dict) and isinstance(language_info.get("name"), str):
        return str(language_info["name"])
    return None


class EpubLocalAdapter:
    """EPUB：按 OPF spine 顺序抽取 XHTML 章节文本。

    - container.xml → OPF → manifest/spine；标题层级进入 heading_path；
    - 限制：条目数/压缩比（复用 archive 选项）；缺失条目记 unit_error；
    - 来源锚点：text_span + part="epub:<entry path>"；无 spine → invalid_epub。
    """

    name = "epub-local"
    version = "0.1.0"
    mode = "local"

    def supports(self, features: ProbeFeatures) -> SupportDecision:
        if features.extension == "epub" or features.mime == "application/epub+zip":
            return SupportDecision(supported=True)
        return SupportDecision(supported=False, reason="非 EPUB")

    def capabilities(self) -> dict[str, object]:
        return {"formats": ["epub"], "unit": "document"}

    def parse(
        self, handle: SourceHandle, options: dict[str, object], execution: ExecutionContext
    ) -> ParseOutcome:
        execution.check_cancelled()
        max_entries = _archive_option(options, "archive_max_entries", 1000)
        max_ratio = _archive_option(options, "archive_max_ratio", 100)
        outcome = ParseOutcome(
            blocks=[], unit_kind="document", engine="epub-zip", engine_version=self.version
        )
        try:
            with zipfile.ZipFile(handle.path) as archive:
                infos = archive.infolist()
                if len(infos) > max_entries:
                    outcome.total_units = 1
                    outcome.succeeded_units = 0
                    outcome.add_unit_error(
                        "document", "archive_too_many_entries", "EPUB 条目数超过上限", retryable=False
                    )
                    return outcome
                compressed = sum(max(info.compress_size, 1) for info in infos)
                declared = sum(info.file_size for info in infos)
                if declared > compressed * max_ratio:
                    outcome.total_units = 1
                    outcome.succeeded_units = 0
                    outcome.add_unit_error(
                        "document", "archive_ratio_exceeded", "EPUB 压缩比超过安全上限", retryable=False
                    )
                    return outcome
                container = safe_fromstring(archive.read("META-INF/container.xml"))
                rootfile = next(
                    (element for element in container.iter() if _local_name(element) == "rootfile"),
                    None,
                )
                opf_path = rootfile.attrib.get("full-path") if rootfile is not None else None
                if not opf_path or opf_path not in archive.namelist():
                    raise KeyError("OPF 不存在")
                opf = safe_fromstring(archive.read(opf_path))
                manifest = {
                    element.attrib.get("id", ""): element.attrib.get("href", "")
                    for element in opf.iter()
                    if _local_name(element) == "item"
                }
                spine_ids = [
                    element.attrib.get("idref", "")
                    for element in opf.iter()
                    if _local_name(element) == "itemref"
                ]
                if not spine_ids:
                    raise ParseFailedError("invalid_epub", "EPUB spine 为空", retryable=False)
                base = posixpath.dirname(opf_path)
                outcome.total_units = len(spine_ids)
                succeeded = 0
                for order, idref in enumerate(spine_ids):
                    execution.check_cancelled()
                    href = manifest.get(idref)
                    entry = _epub_entry_path(base, href)
                    if not entry or entry not in archive.namelist():
                        outcome.add_unit_error(
                            f"spine:{order}",
                            "epub_missing_entry",
                            f"缺少章节条目：{href or idref}",
                            retryable=False,
                        )
                        continue
                    html = archive.read(entry).decode("utf-8", errors="replace")
                    extractor = _XhtmlBlockExtractor()
                    extractor.feed(html)
                    for block in extractor.blocks:
                        for piece in _split_long_text(block["text"]):
                            outcome.blocks.append(
                                ExtractedBlock(
                                    type=block["type"],
                                    text=piece,
                                    source_ref={
                                        "kind": "text_span",
                                        "part": f"epub:{entry}",
                                        "path": entry,
                                        "start": 0,
                                        "end": len(piece),
                                    },
                                    payload=block["payload"],
                                )
                            )
                    succeeded += 1
                outcome.succeeded_units = succeeded
                return outcome
        except (KeyError, zipfile.BadZipFile, ParseError) as exc:
            outcome.total_units = 1
            outcome.succeeded_units = 0
            outcome.add_unit_error(
                "document", "invalid_epub", f"EPUB 结构无法解析：{exc.__class__.__name__}", retryable=False
            )
            return outcome


_EPUB_HEADING_LEVELS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_EPUB_BLOCK_TAGS = {"p", "div", "li", "blockquote", "td", "th", "pre"}


class _XhtmlBlockExtractor(HTMLParser):
    """最小 XHTML 分块器：标题/段落/列表/代码；内联标签不切块。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict[str, Any]] = []
        self._heading_path: list[str] = []
        self._tag: str | None = None
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in _EPUB_BLOCK_TAGS or tag in _EPUB_HEADING_LEVELS:
            self._flush()
            self._tag = tag

    def handle_endtag(self, tag: str) -> None:
        if tag in _EPUB_BLOCK_TAGS or tag in _EPUB_HEADING_LEVELS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._tag is not None:
            self._buffer.append(data)

    def _flush(self) -> None:
        tag = self._tag
        raw = "".join(self._buffer)
        self._tag = None
        self._buffer = []
        if tag is None:
            return
        text = " ".join(raw.split()) if tag != "pre" else raw.strip("\n")
        if not text.strip():
            return
        if tag in _EPUB_HEADING_LEVELS:
            level = _EPUB_HEADING_LEVELS[tag]
            self._heading_path = [*self._heading_path[: level - 1], text.strip()]
            self.blocks.append(
                {
                    "type": "heading",
                    "text": text.strip(),
                    "payload": {"heading_path": list(self._heading_path), "level": level},
                }
            )
            return
        block_type = "code" if tag == "pre" else "list" if tag == "li" else "text"
        self.blocks.append(
            {"type": block_type, "text": text, "payload": {"heading_path": list(self._heading_path)}}
        )


def _local_name(element: Any) -> str:
    return str(element.tag).rsplit("}", 1)[-1]


def _epub_entry_path(base: str, href: str | None) -> str | None:
    if not href:
        return None
    clean = unquote(href.split("#", 1)[0]).lstrip("/")
    if not clean:
        return None
    return posixpath.normpath(posixpath.join(base, clean))


class EmailLocalAdapter:
    """EML（RFC 822）：抽取主题/发件人/收件人/日期与正文；附件只记 warning。

    - 优先 text/plain，仅 HTML 时用最小分块器提取标题/段落；
    - 来源锚点：text_span + part="email:headers"/"email:body"；
    - 无可解析头信息与正文 → empty_email 失败。
    """

    name = "email-local"
    version = "0.1.0"
    mode = "local"

    def supports(self, features: ProbeFeatures) -> SupportDecision:
        if features.extension == "eml" or features.mime == "message/rfc822":
            return SupportDecision(supported=True)
        return SupportDecision(supported=False, reason="非 EML 邮件")

    def capabilities(self) -> dict[str, object]:
        return {"formats": ["eml"], "unit": "document"}

    def parse(
        self, handle: SourceHandle, options: dict[str, object], execution: ExecutionContext
    ) -> ParseOutcome:
        del options
        execution.check_cancelled()
        outcome = ParseOutcome(
            blocks=[], unit_kind="document", engine="email-stdlib", engine_version=self.version
        )
        try:
            with open(handle.path, "rb") as source:
                message = BytesParser(policy=email_policy.default).parse(source)
        except Exception:
            outcome.total_units = 1
            outcome.succeeded_units = 0
            outcome.add_unit_error("document", "invalid_email", "EML 无法解析", retryable=False)
            return outcome

        subject = str(message.get("Subject") or "").strip()
        metadata = [
            (label, str(message.get(header) or "").strip())
            for label, header in (
                ("发件人", "From"),
                ("收件人", "To"),
                ("日期", "Date"),
            )
        ]
        metadata = [(label, value) for label, value in metadata if value]
        blocks: list[ExtractedBlock] = []
        heading_path: list[str] = []
        if subject or metadata:
            title = subject or "(无主题)"
            heading_path = [title]
            blocks.append(
                ExtractedBlock(
                    type="heading",
                    text=title,
                    source_ref={
                        "kind": "text_span",
                        "part": "email:headers",
                        "start": 0,
                        "end": len(title),
                    },
                    payload={"heading_path": list(heading_path), "level": 1},
                )
            )
            if metadata:
                meta_text = "\n".join(f"{label}: {value}" for label, value in metadata)
                blocks.append(
                    ExtractedBlock(
                        type="text",
                        text=meta_text,
                        source_ref={
                            "kind": "text_span",
                            "part": "email:headers",
                            "start": 0,
                            "end": len(meta_text),
                        },
                        payload={"heading_path": list(heading_path)},
                    )
                )

        body_text = ""
        body_html = ""
        for part in message.walk():
            if part.is_multipart():
                continue
            if part.get_content_disposition() == "attachment" or part.get_filename():
                outcome.warnings.append(f"跳过附件：{part.get_filename() or part.get_content_type()}")
                continue
            content_type = part.get_content_type()
            if content_type == "text/plain" and not body_text:
                body_text = _email_part_text(part)
            elif content_type == "text/html" and not body_html:
                body_html = _email_part_text(part)
        if body_text.strip():
            for paragraph in _split_paragraphs(body_text):
                for piece in _split_long_text(paragraph):
                    blocks.append(
                        ExtractedBlock(
                            type="text",
                            text=piece,
                            source_ref={
                                "kind": "text_span",
                                "part": "email:body",
                                "start": 0,
                                "end": len(piece),
                            },
                            payload={"heading_path": list(heading_path)},
                        )
                    )
        elif body_html.strip():
            extractor = _XhtmlBlockExtractor()
            extractor.feed(body_html)
            for block in extractor.blocks:
                block_payload = dict(block["payload"])
                if heading_path:
                    nested = block_payload.get("heading_path")
                    block_payload["heading_path"] = [
                        *heading_path,
                        *(nested if isinstance(nested, list) else []),
                    ]
                for piece in _split_long_text(block["text"]):
                    blocks.append(
                        ExtractedBlock(
                            type=block["type"],
                            text=piece,
                            source_ref={
                                "kind": "text_span",
                                "part": "email:body",
                                "start": 0,
                                "end": len(piece),
                            },
                            payload=block_payload,
                        )
                    )
        if not blocks:
            raise ParseFailedError("empty_email", "邮件没有可解析的头信息或正文", retryable=False)
        outcome.blocks = blocks
        outcome.total_units = 1
        outcome.succeeded_units = 1
        return outcome


def _email_part_text(part: Any) -> str:
    try:
        return str(part.get_content())
    except Exception:
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace")


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "gb18030", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    raise ValueError("无法解码文本文件")


def _split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _parse_csv(handle: SourceHandle, text: str) -> ParseOutcome:
    del handle
    outcome = ParseOutcome(blocks=[], unit_kind="document", engine="builtin-csv", engine_version="0.1.0")
    reader = csv.reader(io.StringIO(text))
    rows: list[list[dict[str, object]]] = []
    for row in reader:
        rows.append([{"text": cell, "row_span": 1, "col_span": 1, "is_header": False} for cell in row])
    table_text = "\n".join(" | ".join(str(c["text"]) for c in row) for row in rows[:200])
    outcome.blocks.append(
        ExtractedBlock(
            type="table",
            text=table_text[:20000],
            source_ref={
                "kind": "table_region",
                "table_id": "table-0",
                "row_start": 0,
                "row_end": len(rows),
                "col_start": 0,
                "col_end": max((len(r) for r in rows), default=0),
            },
            payload={"rows": rows[:2000], "header_row_count": 1 if rows else 0},
        )
    )
    return outcome


class PdfTextLocalAdapter:
    """数字 PDF 文本层提取（pypdf）。

    - 页面级定位：每页文本块使用整页 bbox（页级近似定位，不伪造精确坐标）。
    - text_layer_ratio 由预检计算，路由决定是否降级至 OCR（OCR 为后续阶段）。
    """

    name = "pdf-text-local"
    version = "0.1.0"
    mode = "local"

    def supports(self, features: ProbeFeatures) -> SupportDecision:
        if features.mime == "application/pdf":
            return SupportDecision(supported=True)
        if features.extension == "pdf":
            return SupportDecision(supported=True)
        return SupportDecision(supported=False, reason="非 PDF")

    def capabilities(self) -> dict[str, object]:
        return {"formats": ["pdf"], "unit": "page", "ocr": False}

    def parse(
        self, handle: SourceHandle, options: dict[str, object], execution: ExecutionContext
    ) -> ParseOutcome:
        del options
        try:
            from pypdf import PdfReader
        except ImportError:
            return ParseOutcome(
                blocks=[],
                unit_kind="page",
                engine="pypdf",
                engine_version="missing",
            )
        outcome = ParseOutcome(blocks=[], unit_kind="page", engine="pypdf", engine_version=_pypdf_version())
        try:
            reader = PdfReader(str(handle.path))
        except Exception as exc:
            outcome.total_units = 1
            outcome.succeeded_units = 0
            outcome.add_unit_error("document", "invalid_pdf", f"PDF 无法打开: {exc.__class__.__name__}")
            return outcome
        if reader.is_encrypted:
            try:
                if reader.decrypt("") == 0:  # 空密码尝试；失败即加密文档
                    outcome.total_units = 1
                    outcome.succeeded_units = 0
                    outcome.add_unit_error("document", "encrypted_pdf", "PDF 已加密，无法解析")
                    return outcome
            except Exception:
                outcome.total_units = 1
                outcome.succeeded_units = 0
                outcome.add_unit_error("document", "encrypted_pdf", "PDF 已加密，无法解析")
                return outcome
        total = len(reader.pages)
        outcome.total_units = total
        succeeded = 0
        for page_index, page in enumerate(reader.pages):
            execution.check_cancelled()
            try:
                mediabox = page.mediabox
                width = float(mediabox.width)
                height = float(mediabox.height)
                rotation = int(page.get("/Rotate", 0) or 0) % 360
                outcome.pages.append(
                    ExtractedPage(
                        page_index=page_index,
                        width=width,
                        height=height,
                        rotation=rotation if rotation in (0, 90, 180, 270) else 0,
                    )
                )
                text = (page.extract_text() or "").strip()
            except Exception as exc:
                outcome.add_unit_error(
                    f"page:{page_index}", "page_extract_failed", exc.__class__.__name__, retryable=False
                )
                continue
            succeeded += 1
            if execution.progress is not None:
                execution.progress(page_index + 1, total)
            if not text:
                continue
            for piece in _split_long_text(text):
                outcome.blocks.append(
                    ExtractedBlock(
                        type="text",
                        text=piece,
                        source_ref={
                            "kind": "page_region",
                            "page_index": page_index,
                            "geometry_id": f"page-{page_index}",
                            "bbox": {"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0},
                        },
                        payload={"heading_path": []},
                        confidence_raw=0.9,
                        confidence_method="text-layer-extraction",
                    )
                )
        outcome.succeeded_units = succeeded
        return outcome


def _pypdf_version() -> str:
    try:
        import pypdf

        return str(pypdf.__version__)
    except Exception:
        return "missing"


class DocxLocalAdapter:
    """DOCX：段落/标题层级/表格，使用结构锚点（原生 XML 偏移不做伪精确映射）。"""

    name = "docx-local"
    version = "0.1.0"
    mode = "local"

    def supports(self, features: ProbeFeatures) -> SupportDecision:
        if features.extension == "docx":
            return SupportDecision(supported=True)
        return SupportDecision(supported=False, reason="非 DOCX")

    def capabilities(self) -> dict[str, object]:
        return {"formats": ["docx"], "unit": "document"}

    def parse(
        self, handle: SourceHandle, options: dict[str, object], execution: ExecutionContext
    ) -> ParseOutcome:
        del options
        try:
            import docx
        except ImportError:
            return ParseOutcome(blocks=[], engine="python-docx", engine_version="missing")
        outcome = ParseOutcome(
            blocks=[], engine="python-docx", engine_version=getattr(docx, "__version__", "unknown")
        )
        try:
            document = docx.Document(str(handle.path))
        except Exception as exc:
            outcome.total_units = 1
            outcome.succeeded_units = 0
            outcome.add_unit_error("document", "invalid_docx", exc.__class__.__name__)
            return outcome
        heading_path: list[str] = []
        part = 0
        for paragraph in document.paragraphs:
            execution.check_cancelled()
            text = paragraph.text.strip()
            if not text:
                continue
            style = (paragraph.style.name or "") if paragraph.style else ""
            block_type = "text"
            payload: dict[str, object] = {"heading_path": list(heading_path)}
            level = _docx_heading_level(style)
            if level is not None:
                heading_path = [*heading_path[: level - 1], text]
                block_type = "heading"
                payload = {"heading_path": list(heading_path), "level": level}
            for piece in _split_long_text(text):
                outcome.blocks.append(
                    ExtractedBlock(
                        type=block_type,
                        text=piece,
                        source_ref={
                            "kind": "text_span",
                            "part": "document",
                            "path": f"paragraphs[{part}]",
                            "start": 0,
                            "end": len(piece),
                        },
                        payload=payload,
                    )
                )
            part += 1
        for table_index, table in enumerate(document.tables):
            execution.check_cancelled()
            rows: list[list[dict[str, object]]] = []
            for row in table.rows:
                rows.append(
                    [
                        {"text": cell.text.strip(), "row_span": 1, "col_span": 1, "is_header": False}
                        for cell in row.cells
                    ]
                )
            table_text = "\n".join(" | ".join(str(c["text"]) for c in row) for row in rows)
            outcome.blocks.append(
                ExtractedBlock(
                    type="table",
                    text=table_text[:20000],
                    source_ref={
                        "kind": "table_region",
                        "table_id": f"table-{table_index}",
                        "row_start": 0,
                        "row_end": len(rows),
                        "col_start": 0,
                        "col_end": max((len(r) for r in rows), default=0),
                    },
                    payload={"rows": rows[:2000], "header_row_count": 1 if rows else 0},
                )
            )
        return outcome


def _docx_heading_level(style_name: str) -> int | None:
    match = re.match(r"Heading (\d+)|标题 (\d+)", style_name)
    if match:
        return int(match.group(1) or match.group(2))
    return None


class PptxLocalAdapter:
    """PPTX：以 slide 为结构单位；检索块拆分由分块阶段按预算执行。"""

    name = "pptx-local"
    version = "0.1.0"
    mode = "local"

    def supports(self, features: ProbeFeatures) -> SupportDecision:
        if features.extension == "pptx":
            return SupportDecision(supported=True)
        return SupportDecision(supported=False, reason="非 PPTX")

    def capabilities(self) -> dict[str, object]:
        return {"formats": ["pptx"], "unit": "slide"}

    def parse(
        self, handle: SourceHandle, options: dict[str, object], execution: ExecutionContext
    ) -> ParseOutcome:
        del options
        try:
            from pptx import Presentation
        except ImportError:
            return ParseOutcome(blocks=[], engine="python-pptx", engine_version="missing")
        outcome = ParseOutcome(
            blocks=[], engine="python-pptx", engine_version=_pptx_version(), unit_kind="document"
        )
        try:
            presentation = Presentation(str(handle.path))
        except Exception as exc:
            outcome.total_units = 1
            outcome.succeeded_units = 0
            outcome.add_unit_error("document", "invalid_pptx", exc.__class__.__name__)
            return outcome
        for slide_index, slide in enumerate(presentation.slides):
            execution.check_cancelled()
            heading_text = ""
            body_texts: list[str] = []
            for shape in slide.shapes:
                if not shape.has_text_frame:
                    continue
                for paragraph in shape.text_frame.paragraphs:
                    text = "".join(run.text for run in paragraph.runs).strip()
                    if not text:
                        continue
                    if not heading_text:
                        heading_text = text
                    else:
                        body_texts.append(text)
            if heading_text:
                outcome.blocks.append(
                    ExtractedBlock(
                        type="heading",
                        text=heading_text,
                        source_ref={
                            "kind": "text_span",
                            "part": f"slide:{slide_index}",
                            "start": 0,
                            "end": len(heading_text),
                        },
                        payload={"heading_path": [heading_text], "level": 1},
                    )
                )
            for text in body_texts:
                for piece in _split_long_text(text):
                    outcome.blocks.append(
                        ExtractedBlock(
                            type="text",
                            text=piece,
                            source_ref={
                                "kind": "text_span",
                                "part": f"slide:{slide_index}",
                                "start": 0,
                                "end": len(piece),
                            },
                            payload={"heading_path": [heading_text] if heading_text else []},
                        )
                    )
        return outcome


def _pptx_version() -> str:
    try:
        import pptx

        return str(pptx.__version__)
    except Exception:
        return "missing"


class XlsxLocalAdapter:
    """XLSX：保留 sheet、单元格区域与合并关系；公式存取为文本。"""

    name = "xlsx-local"
    version = "0.1.0"
    mode = "local"

    def supports(self, features: ProbeFeatures) -> SupportDecision:
        if features.extension in {"xlsx", "xlsm"}:
            return SupportDecision(supported=True)
        return SupportDecision(supported=False, reason="非 XLSX")

    def capabilities(self) -> dict[str, object]:
        return {"formats": ["xlsx", "xlsm"], "unit": "sheet"}

    def parse(
        self, handle: SourceHandle, options: dict[str, object], execution: ExecutionContext
    ) -> ParseOutcome:
        del options
        try:
            from openpyxl import load_workbook
        except ImportError:
            return ParseOutcome(blocks=[], engine="openpyxl", engine_version="missing")
        outcome = ParseOutcome(
            blocks=[],
            engine="openpyxl",
            engine_version=_openpyxl_version(),
            unit_kind="document",
        )
        try:
            workbook = load_workbook(str(handle.path), read_only=True, data_only=True)
        except Exception as exc:
            outcome.total_units = 1
            outcome.succeeded_units = 0
            outcome.add_unit_error("document", "invalid_xlsx", exc.__class__.__name__)
            return outcome
        for sheet in workbook.worksheets:
            execution.check_cancelled()
            rows: list[list[dict[str, object]]] = []
            for row in sheet.iter_rows(max_row=2000, max_col=100, values_only=True):
                if row is None or all(cell is None for cell in row):
                    continue
                rows.append(
                    [
                        {
                            "text": "" if cell is None else str(cell),
                            "row_span": 1,
                            "col_span": 1,
                            "is_header": False,
                        }
                        for cell in row
                    ]
                )
            table_text = "\n".join(" | ".join(str(c["text"]) for c in row) for row in rows)
            outcome.blocks.append(
                ExtractedBlock(
                    type="table",
                    text=(table_text or sheet.title)[:20000],
                    source_ref={
                        "kind": "table_region",
                        "table_id": f"sheet:{sheet.title}",
                        "sheet_name": sheet.title,
                        "row_start": 0,
                        "row_end": len(rows),
                        "col_start": 0,
                        "col_end": max((len(r) for r in rows), default=0),
                    },
                    payload={"rows": rows, "header_row_count": 1 if rows else 0},
                )
            )
        if not outcome.blocks:
            outcome.total_units = 1
        return outcome


def _openpyxl_version() -> str:
    try:
        import openpyxl

        return str(openpyxl.__version__)
    except Exception:
        return "missing"


def default_registry() -> dict[str, ParserAdapter]:
    """内置 adapter 注册表（name -> instance）。

    外部插件经 entry_points 组 `owli_parser.parsers` 注册，由部署流程安装并锁版本。
    """
    return cast(
        dict[str, ParserAdapter],
        {
            adapter.name: adapter
            for adapter in (
                TextLocalAdapter(),
                ZipLocalAdapter(),
                JupyterLocalAdapter(),
                EpubLocalAdapter(),
                EmailLocalAdapter(),
                PdfTextLocalAdapter(),
                DocxLocalAdapter(),
                PptxLocalAdapter(),
                XlsxLocalAdapter(),
            )
        },
    )
