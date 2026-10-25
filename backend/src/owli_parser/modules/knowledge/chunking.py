"""分块引擎：结构优先 + token 预算（父子双粒度、表格行组视图）。

- 结构优先：heading 边界；heading_path 与 source_refs 必须保留。
- token 预算：tokenizer 为可配置引用；内置 approx:chars4-v1 为明确标注的近似。
- 表格：完整表格独立保存为父块语义；检索表示按 token 预算分行组并复制表头。
- 语义切分首期非必需（M3 最低闭环不含句向量断点）。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from owli_parser.bootstrap.settings import ChunkSection
from owli_parser.contracts.ir import Block, DocumentIR, TablePayload


def count_tokens(text: str, tokenizer_ref: str) -> int:
    if tokenizer_ref == "approx:chars4-v1":
        return max(1, math.ceil(len(text) / 4)) if text else 0
    # 未识别的 tokenizer 引用：以保守估算执行并明确出现在 explanation 中
    return max(1, math.ceil(len(text) / 2)) if text else 0


@dataclass
class NewChunk:
    ordinal: int
    kind: str  # parent / child
    text: str
    token_count: int
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    parent_ordinal: int | None = None
    explanation: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True


def build_chunks(ir: DocumentIR, settings: ChunkSection) -> list[NewChunk]:
    """把 DocumentIR 切分为父子两级 chunk 列表（父块在前按 occur 顺序）。"""
    tokenizer = settings.tokenizer_ref
    chunks: list[NewChunk] = []
    ordinal = 0

    # 按 heading 边界分组（结构优先）
    sections: list[tuple[list[str], list[Block]]] = []
    current_path: list[str] = []
    current_blocks: list[Block] = []
    for block in sorted(ir.blocks, key=lambda b: b.order):
        if block.type == "heading":
            if current_blocks:
                sections.append((current_path, current_blocks))
                current_blocks = []
            level = int(block.payload.__dict__.get("level") or len(current_path) + 1)
            current_path = [*current_path[: max(level - 1, 0)], block.text.strip()]
        current_blocks.append(block)
    if current_blocks:
        sections.append((current_path, current_blocks))

    for heading_path, blocks in sections:
        if not blocks:
            continue
        section_text = "\n".join(b.text for b in blocks if b.text)
        if not section_text.strip():
            continue
        parent_ordinal = ordinal
        parent_tokens = count_tokens(section_text, tokenizer)
        chunks.append(
            NewChunk(
                ordinal=ordinal,
                kind="parent",
                text=section_text[: settings.parent_max_tokens * 4],
                token_count=min(parent_tokens, settings.parent_max_tokens),
                source_refs=_refs_of(blocks),
                explanation={
                    "kind": "section",
                    "heading_path": heading_path,
                    "total_tokens": parent_tokens,
                },
            )
        )
        ordinal += 1

        # 子块：按段落/表格累积到 target_tokens
        pending: list[Block] = []
        pending_tokens = 0

        def flush() -> int | None:
            nonlocal ordinal, pending, pending_tokens
            if not pending:
                return None
            text = "\n".join(b.text for b in pending if b.text)
            if not text.strip():
                pending = []
                pending_tokens = 0
                return None
            chunk_ordinal = ordinal
            chunks.append(
                NewChunk(
                    ordinal=ordinal,
                    kind="child",
                    text=text,
                    token_count=count_tokens(text, tokenizer),
                    source_refs=_refs_of(pending),
                    parent_ordinal=parent_ordinal,  # noqa: B023 - 闭包同轮内立即调用
                    explanation={
                        "kind": "paragraph_group",
                        "heading_path": heading_path,  # noqa: B023
                        "block_ids": [b.id for b in pending],
                        "split_reason": "structure",
                        "tokenizer_ref": tokenizer,
                    },
                )
            )
            ordinal += 1
            pending = []
            pending_tokens = 0
            return chunk_ordinal

        for block in blocks:
            if block.type == "heading":
                continue
            if block.type == "table":
                flush()
                table_ordinal = _emit_table_chunks(
                    block, heading_path, chunks, ordinal, parent_ordinal, settings
                )
                ordinal = table_ordinal
                continue
            block_tokens = count_tokens(block.text, tokenizer)
            if block_tokens >= settings.max_tokens:
                flush()
                pieces = _split_text_by_budget(block.text, settings, tokenizer)
                for piece in pieces:
                    chunks.append(
                        NewChunk(
                            ordinal=ordinal,
                            kind="child",
                            text=piece,
                            token_count=count_tokens(piece, tokenizer),
                            source_refs=[ref.model_dump(mode="json") for ref in block.source_refs],
                            parent_ordinal=parent_ordinal,
                            explanation={
                                "kind": "token_split",
                                "heading_path": heading_path,
                                "split_reason": "token_budget",
                                "tokenizer_ref": tokenizer,
                            },
                        )
                    )
                    ordinal += 1
                continue
            if pending and pending_tokens + block_tokens > settings.target_tokens:
                flush()
            pending.append(block)
            pending_tokens += block_tokens
        flush()
    # 重排 ordinal 保持顺序
    for index, chunk in enumerate(chunks):
        chunk.ordinal = index
    return chunks


def _emit_table_chunks(
    block: Block,
    heading_path: list[str],
    chunks: list[NewChunk],
    ordinal: int,
    parent_ordinal: int,
    settings: ChunkSection,
) -> int:
    """完整表格独立保存（父语义）；检索表示按行组切分并复制表头。"""
    refs = [ref.model_dump(mode="json") for ref in block.source_refs]
    payload = block.payload if isinstance(block.payload, TablePayload) else None
    rows = payload.rows if payload is not None else []
    header_count = payload.header_row_count if payload is not None else 0
    header_text = (
        "\n".join(" | ".join(c.text for c in row) for row in rows[:header_count]) if header_count else ""
    )
    body_rows = rows[header_count:] or rows
    # 完整表格文本（以结构行渲染；block.text 可能只是截断摘要）
    full_text = "\n".join(" | ".join(c.text for c in row) for row in rows) or block.text
    # 完整表格视图
    chunks.append(
        NewChunk(
            ordinal=ordinal,
            kind="child",
            text=full_text[:20000],
            token_count=count_tokens(full_text, settings.tokenizer_ref),
            source_refs=refs,
            parent_ordinal=parent_ordinal,
            explanation={
                "kind": "table_full",
                "heading_path": heading_path,
                "table_id": refs[0].get("table_id") if refs else None,
                "row_count": len(rows),
                "note": "完整表格作为结构化产物保留",
            },
        )
    )
    ordinal += 1
    # 行组检索表示（仅在完整表格超过预算时拆分）
    if count_tokens(full_text, settings.tokenizer_ref) <= settings.max_tokens:
        return ordinal
    group: list[list[Any]] = []
    group_tokens = count_tokens(header_text, settings.tokenizer_ref)
    consumed_body = 0

    def flush_group() -> None:
        nonlocal ordinal, group, group_tokens, consumed_body
        if not group:
            return
        text_parts = [header_text] if header_text else []
        text_parts.extend(" | ".join(c.text for c in row) for row in group)
        text = "\n".join(part for part in text_parts if part)
        row_start = header_count + consumed_body
        chunks.append(
            NewChunk(
                ordinal=ordinal,
                kind="child",
                text=text,
                token_count=count_tokens(text, settings.tokenizer_ref),
                source_refs=refs,
                parent_ordinal=parent_ordinal,
                explanation={
                    "kind": "table_row_group",
                    "heading_path": heading_path,
                    "header_copied": bool(header_text),
                    "row_start": row_start,
                    "row_end": row_start + len(group),
                    "split_reason": "table_token_budget",
                },
            )
        )
        ordinal += 1
        consumed_body += len(group)
        group = []
        group_tokens = count_tokens(header_text, settings.tokenizer_ref)

    for row in body_rows:
        row_text = " | ".join(c.text for c in row)
        row_tokens = count_tokens(row_text, settings.tokenizer_ref)
        if group and group_tokens + row_tokens > settings.table_row_group_target_tokens:
            flush_group()
        group.append(row)
        group_tokens += row_tokens
    flush_group()
    return ordinal


def _split_text_by_budget(text: str, settings: ChunkSection, tokenizer: str) -> list[str]:
    target_chars = settings.target_tokens * 4
    max_chars = settings.max_tokens * 4
    overlap_chars = settings.overlap_tokens * 4
    pieces: list[str] = []
    start = 0
    total = len(text)
    while start < total:
        end = min(start + max_chars, total)
        cut = text.rfind("\n", start + target_chars, end) if end < total else -1
        if cut <= start:
            cut = min(start + target_chars, total)
        piece = text[start:cut]
        if piece.strip():
            pieces.append(piece)
        if cut >= total:
            break
        start = max(cut - overlap_chars, start + 1)
    del tokenizer
    return pieces


def _refs_of(blocks: list[Block]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for block in blocks:
        for ref in block.source_refs:
            payload = ref.model_dump(mode="json")
            payload["block_id"] = block.id
            refs.append(payload)
    return refs


def canonical_ids_digest(ids: list[str]) -> str:
    import hashlib

    canonical = json.dumps(sorted(ids), separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
