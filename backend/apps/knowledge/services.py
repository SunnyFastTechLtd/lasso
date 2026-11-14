"""
知识库文档处理服务

文档上传后的处理流水线：文本提取 → 分块 → 向量化 → 落库，
以及面向检索的向量相似度查询。
"""
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List

import numpy as np
from django.conf import settings
from docx import Document as DocxDocument
from openpyxl import load_workbook
from pypdf import PdfReader
from pptx import Presentation

# 保留 embeddings 名字以兼容既有调用/测试 patch，实际为本地 fastembed
from backend.apps.common.embedding import embed_texts as embeddings
from .models import DocumentChunk
from . import ocr, vectorstore

logger = logging.getLogger(__name__)

# P1 图片入库：按图片走 OCR 的文件类型
IMAGE_TYPES = {'png', 'jpg', 'jpeg', 'webp', 'bmp', 'tif', 'tiff'}


def split_text(text: str, chunk_size: int = 500, overlap: int = 50) -> List[str]:
    """
    将长文本按定长 + 重叠切分为有序分块

    Args:
        text: 原始文本
        chunk_size: 每块最大字符数
        overlap: 相邻块重叠字符数

    Returns:
        有序分块列表，去除重叠后按序拼接可还原全文
    """
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    step = chunk_size - overlap
    start = 0
    while start < len(text):
        chunks.append(text[start:start + chunk_size])
        if start + chunk_size >= len(text):
            break
        start += step
    return chunks


def extract_text(file_path: str, file_type: str) -> str:
    """
    从文件中提取纯文本

    Args:
        file_path: 文件磁盘路径
        file_type: 文件类型（txt / md / pdf / docx / xlsx / pptx）

    Returns:
        提取出的纯文本

    Raises:
        ValueError: 不支持的文件类型
    """
    if file_type in ('txt', 'md'):
        with open(file_path, encoding='utf-8') as f:
            return f.read()
    if file_type == 'pdf':
        reader = PdfReader(file_path)
        return '\n'.join(page.extract_text() or '' for page in reader.pages)
    if file_type == 'docx':
        return _extract_docx_text(file_path)
    if file_type == 'xlsx':
        return _extract_xlsx_text(file_path)
    if file_type == 'pptx':
        return _extract_pptx_text(file_path)
    raise ValueError(f'不支持的文件类型：{file_type}（支持 txt/md/pdf/docx/xlsx/pptx）')


def _extract_docx_text(file_path: str) -> str:
    """Word 文档：段落 + 表格（行内单元格用 | 分隔）"""
    doc = DocxDocument(file_path)
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            parts.append(' | '.join(cell.text.strip() for cell in row.cells))
    return '\n'.join(parts)


def _extract_xlsx_text(file_path: str) -> str:
    """Excel 工作簿：逐 sheet 提取，行内单元格用制表符分隔"""
    wb = load_workbook(file_path, read_only=True, data_only=True)
    try:
        lines = []
        for ws in wb.worksheets:
            lines.append(f'## {ws.title}')
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) if c is not None else '' for c in row]
                if any(c.strip() for c in cells):
                    lines.append('\t'.join(cells).rstrip())
        return '\n'.join(lines)
    finally:
        wb.close()


def _extract_pptx_text(file_path: str) -> str:
    """PPT 演示文稿：逐页提取文本框与表格"""
    prs = Presentation(file_path)
    lines = []
    for i, slide in enumerate(prs.slides, start=1):
        lines.append(f'## 第 {i} 页')
        for shape in slide.shapes:
            if shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    lines.append(text)
            if shape.has_table:
                for row in shape.table.rows:
                    lines.append(' | '.join(cell.text.strip() for cell in row.cells))
    return '\n'.join(lines)


@dataclass
class PageText:
    """一页的提取结果；page=None 表示非分页格式（docx/md/txt…）"""

    page: int | None
    text: str = ''
    ocr: bool = False
    engine: str = ''
    status: str = 'ok'      # ok / failed
    reason: str = ''


def clean_text(text: str) -> str:
    """OCR 正文与文字层正文共用的清洗口径：NFKC、去控制符、压空白、去多余空行

    页码/OCR 等标记不进正文，避免污染关键词与向量。
    """
    if not text:
        return ''
    text = unicodedata.normalize('NFKC', text)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = ''.join(ch for ch in text if ch in '\n\t' or ord(ch) >= 32)
    lines = [re.sub(r'[ \t\u00a0]+', ' ', line).strip() for line in text.split('\n')]
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()


def _min_ocr_chars() -> int:
    return int(getattr(settings, 'KNOWLEDGE_OCR_MIN_TEXT_CHARS', 16))


def _build_report(pages: List[PageText], mode: str) -> Dict[str, Any]:
    """逐页报告：文字层/OCR/未解析原因；mode=text 表示非分页格式"""
    paged = mode != 'text'
    rows = [{
        'page': page.page,
        'chars': len(page.text),
        'text_layer': bool(page.text) and not page.ocr,
        'ocr': page.ocr,
        'engine': page.engine,
        'status': page.status,
        'reason': page.reason,
    } for page in pages] if paged else []
    return {
        'mode': mode,
        'ocr_used': any(page.ocr for page in pages),
        'pages_total': len(pages) if paged else 0,
        'ocr_pages': [page.page for page in pages if page.ocr],
        'failed_pages': [{'page': page.page, 'reason': page.reason}
                         for page in pages if page.status == 'failed'],
        'pages': rows,
    }


def _empty_error(report: Dict[str, Any]) -> str:
    failed = report.get('failed_pages') or []
    if failed:
        pages = '、'.join(str(item['page']) for item in failed[:10])
        reasons = '；'.join(sorted({item['reason'] for item in failed if item.get('reason')}))
        return f'未能提取到可检索文本：第 {pages} 页未解析（{reasons}）'
    return '未能提取到可检索文本（文档无可识别文字）'


def extract_pages(file_path: str, file_type: str,
                  enable_ocr: bool = True) -> tuple:
    """按格式提取，逐页返回 (List[PageText], extraction_report)

    PDF 逐页判断：有文字层直接用；无文字层（或过短）才渲染成图走 OCR。
    enable_ocr=False 时只取文字层（对话临时附件路径）。
    """
    file_type = (file_type or '').lower()
    if file_type == 'pdf':
        return _extract_pdf_pages(file_path, enable_ocr)
    if file_type in IMAGE_TYPES:
        return _extract_image_pages(file_path, file_type, enable_ocr)
    text = clean_text(extract_text(file_path, file_type))
    pages = [PageText(page=None, text=text)]
    return pages, _build_report(pages, 'text')


def _extract_pdf_pages(file_path: str, enable_ocr: bool) -> tuple:
    reader = PdfReader(file_path)
    max_pages = int(getattr(settings, 'KNOWLEDGE_OCR_MAX_PAGES', 200))
    pages: List[PageText] = []
    for index, pdf_page in enumerate(reader.pages):
        page_no = index + 1
        if page_no > max_pages:
            pages.append(PageText(page=page_no, status='failed', reason='max_pages_exceeded'))
            break
        text = clean_text(pdf_page.extract_text() or '')
        if len(text) >= _min_ocr_chars():
            pages.append(PageText(page=page_no, text=text))
            continue
        if not enable_ocr:
            pages.append(PageText(page=page_no, text=text,
                                  status='ok' if text else 'failed',
                                  reason='ocr_disabled'))
            continue

        outcome = ocr.ocr_pdf_page(file_path, index)
        ocr_text = clean_text(outcome.text)
        if ocr_text:
            pages.append(PageText(page=page_no, text=ocr_text, ocr=True,
                                  engine=outcome.engine))
        elif text:
            # 文字层很短且 OCR 失败：保留短文字层不丢内容，report 里记原因
            pages.append(PageText(page=page_no, text=text,
                                  reason=f'ocr_failed:{outcome.error}'))
        else:
            pages.append(PageText(page=page_no, status='failed',
                                  reason=outcome.error or 'ocr_empty'))
    return pages, _build_report(pages, 'auto')


def _extract_image_pages(file_path: str, file_type: str, enable_ocr: bool) -> tuple:
    if not enable_ocr:
        pages = [PageText(page=1, status='failed', reason='ocr_disabled')]
        return pages, _build_report(pages, 'auto')
    outcome = ocr.ocr_image_file(file_path, file_type)
    text = clean_text(outcome.text)
    if text:
        pages = [PageText(page=1, text=text, ocr=True, engine=outcome.engine)]
    else:
        pages = [PageText(page=1, status='failed', reason=outcome.error or 'ocr_empty')]
    return pages, _build_report(pages, 'auto')


def process_document(doc, enable_ocr: bool = True) -> None:
    """
    文档处理流水线：提取文本/OCR → 分块 → 向量化 → 落库

    PDF 逐页判定（混合文档逐页各走各的）；无任何可检索文本时标记 failed 并写明
    哪几页未解析，绝不出现 completed 但零切片的空壳。

    Args:
        doc: knowledge.Document 实例
        enable_ocr: 知识库入库为 True；对话临时附件路径传 False，不走 OCR
    """
    doc.status = 'processing'
    doc.save(update_fields=['status'])

    deleted_chunks = 0
    try:
        # 幂等：重跑前先清理旧分块和旧向量，避免重复入库；
        # 记录实际清理数，用于后面按差额校准 kb.chunk_count（否则重跑会重复累加）
        deleted_chunks, _ = doc.chunks.all().delete()
        if vectorstore.is_enabled():
            try:
                vectorstore.delete_document_vectors(doc.knowledge_base_id, doc.id)
            except Exception as e:
                logger.warning('清理 Qdrant 旧向量失败（doc=%s）：%s', doc.id, e)

        pages, report = extract_pages(doc.file_path.path, doc.file_type,
                                      enable_ocr=enable_ocr)
        entries = []
        for page in pages:
            if page.status != 'ok' or not page.text.strip():
                continue
            meta: Dict[str, Any] = {}
            if page.page:
                meta['page'] = page.page
            if page.ocr:
                meta['ocr'] = True
                meta['ocr_engine'] = page.engine
            for content in split_text(page.text):
                entries.append((content, meta))

        # 去重：OCR 页与文字层页同一口径，保留首次出现
        seen = set()
        unique = []
        for content, meta in entries:
            if content in seen:
                continue
            seen.add(content)
            unique.append((content, meta))
        contents = [content for content, _ in unique]
        metas = [meta for _, meta in unique]
        vectors = embeddings(contents) if contents else []

        if not contents:
            doc.status = 'failed'
            doc.chunk_count = 0
            doc.error_msg = _empty_error(report)
            doc.extraction_report = report
            doc.save(update_fields=['status', 'chunk_count', 'error_msg',
                                    'extraction_report'])
            kb = doc.knowledge_base
            kb.chunk_count = max(0, kb.chunk_count - deleted_chunks)
            kb.save(update_fields=['chunk_count'])
            return

        chunks = DocumentChunk.objects.bulk_create([
            DocumentChunk(
                document=doc,
                chunk_index=i,
                content=content,
                embedding=vector,
                meta=metas[i],
            )
            for i, (content, vector) in enumerate(zip(contents, vectors))
        ])

        if vectorstore.is_enabled() and chunks:
            vectorstore.upsert_chunks(
                doc.knowledge_base_id,
                [(c.id, c.embedding, doc.id) for c in chunks],
            )

        doc.status = 'completed'
        doc.chunk_count = len(contents)
        doc.error_msg = ''
        doc.extraction_report = report
        doc.save(update_fields=['status', 'chunk_count', 'error_msg',
                                'extraction_report'])

        kb = doc.knowledge_base
        # 按新旧差额校准：重跑场景先清后建，只加净增量
        kb.chunk_count = max(0, kb.chunk_count + len(contents) - deleted_chunks)
        kb.save(update_fields=['chunk_count'])
    except Exception as e:
        # 异常可能发生在旧分块已清、新分块未完全落库之间：兜底清掉本次残留，
        # 让 chunk_count 与表内实际行数一致，不留下半份索引。
        doc.chunks.all().delete()
        doc.status = 'failed'
        doc.error_msg = str(e)
        doc.chunk_count = 0
        doc.save(update_fields=['status', 'error_msg', 'chunk_count'])
        kb = doc.knowledge_base
        kb.chunk_count = max(0, kb.chunk_count - deleted_chunks)
        kb.save(update_fields=['chunk_count'])


def search_chunks(knowledge_base_id: int, query_vector: List[float], top_k: int = 5) -> List[Dict[str, Any]]:
    """按向量相似度在指定知识库内检索分块，降序返回前 top_k 条

    VECTOR_STORE=qdrant 时走 Qdrant ANN 检索（命中后按 id 回表取内容），
    否则回退 DB 内存余弦全表扫描（数据量小/无 Qdrant 时用）。
    """
    if vectorstore.is_enabled():
        return _search_chunks_qdrant(knowledge_base_id, query_vector, top_k)
    return _search_chunks_local(knowledge_base_id, query_vector, top_k)


def _search_chunks_qdrant(knowledge_base_id: int, query_vector: List[float], top_k: int) -> List[Dict[str, Any]]:
    """Qdrant ANN 检索分支"""
    scored = vectorstore.search(knowledge_base_id, query_vector, top_k)
    if not scored:
        return []

    chunks_by_id = vectorstore.build_chunks_map([cid for cid, _ in scored])
    results = []
    for chunk_id, score in scored:
        chunk = chunks_by_id.get(chunk_id)
        if chunk is None:
            continue  # Qdrant 中的孤儿向量（分块已删），跳过
        if not chunk.is_active:
            continue  # 禁检分块（禁检时向量已从 Qdrant 移除，这里双保险）
        results.append({
            'chunk_id': chunk.id,
            'chunk_index': chunk.chunk_index,
            'content': chunk.content,
            'score': score,
            'doc_id': chunk.document_id,
            'doc_title': chunk.document.title,
            'file_name': chunk.document.file_name,
            'meta': chunk.meta or {},
        })
    return results


def _search_chunks_local(knowledge_base_id: int, query_vector: List[float], top_k: int) -> List[Dict[str, Any]]:
    """DB 内存余弦检索分支（全表扫描，仅适合小数据量）"""
    chunks = list(
        DocumentChunk.objects.filter(
            document__knowledge_base_id=knowledge_base_id,
            document__status='completed',
            is_active=True,
        )
        .exclude(embedding=[])
        .select_related('document')
    )
    if not chunks:
        return []

    q = np.asarray(query_vector, dtype=float)
    q_norm = np.linalg.norm(q)
    results = []
    for chunk in chunks:
        v = np.asarray(chunk.embedding, dtype=float)
        denom = q_norm * np.linalg.norm(v)
        score = float(np.dot(q, v) / denom) if denom else 0.0
        results.append({
            'chunk_id': chunk.id,
            'chunk_index': chunk.chunk_index,
            'content': chunk.content,
            'score': score,
            'doc_id': chunk.document_id,
            'doc_title': chunk.document.title,
            'file_name': chunk.document.file_name,
            'meta': chunk.meta or {},
        })
    results.sort(key=lambda r: r['score'], reverse=True)
    return results[:top_k]
