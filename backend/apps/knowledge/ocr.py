"""知识库 OCR（P0 扫描 PDF / P1 图片）

方案 A：auto 三级回退 —— 多模态模型优先 → 本机 tesseract → 明确报未解析。
- 多模态模型复用平台现有 LLM 网关与已配置模型（不新增模型配置项）；
- tesseract 未安装/缺中文语言包时不抛异常，由调用方把该页记成 failed；
- 不在对话临时附件路径上调用（对话侧仍走 services.extract_text，enable_ocr=False）。
"""
import base64
import logging
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import List

from django.conf import settings

logger = logging.getLogger(__name__)

_LLM_PROMPT = (
    '你是 OCR 引擎。逐字提取图片中的全部文字，保持原有分段与换行。'
    '不要总结、不要翻译、不要解释、不要输出 Markdown 标记。'
    '如果图片里没有可识别文字，只输出空内容。'
)

_langs_cache = {}
_langs_lock = threading.Lock()


@dataclass
class OcrOutcome:
    """单页 OCR 结果；attempts 记录每一级的尝试与失败原因，供 extraction_report 使用"""

    text: str = ''
    engine: str = ''          # llm / tesseract / ''（都失败）
    error: str = ''           # 都失败时的汇总原因（短码）
    attempts: List[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.text.strip())


def _setting(name: str, default):
    return getattr(settings, name, default)


def tesseract_status() -> tuple:
    """返回 (可用, 原因)；结果按 binary+langs 缓存，避免每页都跑子进程"""
    binary = shutil.which('tesseract')
    if not binary:
        return False, 'tesseract_not_installed'
    langs = _setting('KNOWLEDGE_OCR_LANG', 'chi_sim+eng')
    key = (binary, langs)
    with _langs_lock:
        if key in _langs_cache:
            return _langs_cache[key]
    try:
        proc = subprocess.run([binary, '--list-langs'], capture_output=True,
                              text=True, timeout=10)
        installed = {line.strip() for line in (proc.stdout or '').splitlines()
                     if line.strip() and not line.lower().startswith('list of available')}
    except Exception as exc:  # noqa: BLE001 - 探测失败按不可用处理
        result = (False, f'tesseract_probe_failed:{type(exc).__name__}')
        with _langs_lock:
            _langs_cache[key] = result
        return result
    missing = [lang for lang in langs.split('+') if lang and lang not in installed]
    result = (False, f'tesseract_missing_lang:{",".join(missing)}') if missing else (True, '')
    with _langs_lock:
        _langs_cache[key] = result
    return result


def _llm_ocr(data: bytes, mime: str) -> tuple:
    """一级：多模态模型。返回 (text, error)"""
    from backend.apps.common import llm

    url = f'data:{mime};base64,{base64.b64encode(data).decode("ascii")}'
    messages = [{
        'role': 'user',
        'content': [
            {'type': 'text', 'text': _LLM_PROMPT},
            {'type': 'image_url', 'image_url': {'url': url}},
        ],
    }]
    try:
        text = llm.chat_completion(
            messages=messages,
            temperature=0,
            max_tokens=_setting('KNOWLEDGE_OCR_LLM_MAX_TOKENS', 4096),
            timeout=_setting('KNOWLEDGE_OCR_LLM_TIMEOUT', 60),
        )
    except llm.ModelConfigError:
        return '', 'llm_not_configured'
    except Exception as exc:  # noqa: BLE001 - 降级到 tesseract，不中断整份文档
        logger.info('多模态 OCR 失败，准备降级 tesseract：%s', exc)
        return '', f'llm_failed:{type(exc).__name__}'
    return (text or '').strip(), ''


def _tesseract_ocr(data: bytes) -> tuple:
    """二级：本机 tesseract。返回 (text, error)"""
    available, reason = tesseract_status()
    if not available:
        return '', reason
    try:
        proc = subprocess.run(
            ['tesseract', 'stdin', 'stdout', '-l', _setting('KNOWLEDGE_OCR_LANG', 'chi_sim+eng'),
             '--psm', '6'],
            input=data, capture_output=True,
            timeout=_setting('KNOWLEDGE_OCR_TIMEOUT', 60),
        )
    except subprocess.TimeoutExpired:
        return '', 'tesseract_timeout'
    except Exception as exc:  # noqa: BLE001
        return '', f'tesseract_failed:{type(exc).__name__}'
    if proc.returncode != 0:
        detail = (proc.stderr or b'').decode('utf-8', errors='replace').strip().splitlines()
        return '', f'tesseract_failed:{(detail[-1] if detail else proc.returncode)}'[:160]
    text = (proc.stdout or b'').decode('utf-8', errors='replace').strip()
    return (text, '') if text else ('', 'tesseract_empty')


def ocr_image_bytes(data: bytes, mime: str = 'image/png') -> OcrOutcome:
    """auto 三级回退：多模态模型 → tesseract → 失败（带逐级原因）"""
    outcome = OcrOutcome()
    text, error = _llm_ocr(data, mime)
    outcome.attempts.append({'engine': 'llm', 'ok': bool(text), 'error': error})
    if text:
        outcome.text, outcome.engine = text, 'llm'
        return outcome

    text, error = _tesseract_ocr(data)
    outcome.attempts.append({'engine': 'tesseract', 'ok': bool(text), 'error': error})
    if text:
        outcome.text, outcome.engine = text, 'tesseract'
        return outcome

    outcome.error = '；'.join(
        f'{item["engine"]}={item["error"]}' for item in outcome.attempts if item['error']
    ) or 'ocr_failed'
    return outcome


def render_pdf_page(file_path: str, page_index: int) -> bytes:
    """把 PDF 单页渲染成 PNG（pypdfium2，约 144 DPI）"""
    import pypdfium2 as pdfium

    scale = float(_setting('KNOWLEDGE_OCR_RENDER_SCALE', 2.0))
    document = pdfium.PdfDocument(file_path)
    try:
        page = document[page_index]
        try:
            bitmap = page.render(scale=scale)
            try:
                image = bitmap.to_pil()
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        document.close()
    buffer = __import__('io').BytesIO()
    image.save(buffer, format='PNG', optimize=True)
    return buffer.getvalue()


def ocr_pdf_page(file_path: str, page_index: int) -> OcrOutcome:
    try:
        data = render_pdf_page(file_path, page_index)
    except Exception as exc:  # noqa: BLE001 - 渲染失败也按该页未解析处理
        logger.warning('PDF 第 %s 页渲染失败：%s', page_index + 1, exc)
        return OcrOutcome(error=f'render_failed:{type(exc).__name__}')
    return ocr_image_bytes(data, 'image/png')


MIME_BY_EXT = {
    'png': 'image/png',
    'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg',
    'webp': 'image/webp',
    'bmp': 'image/bmp',
    'tif': 'image/tiff',
    'tiff': 'image/tiff',
}


def ocr_image_file(file_path: str, file_type: str) -> OcrOutcome:
    mime = MIME_BY_EXT.get(file_type.lower(), 'image/png')
    try:
        with open(file_path, 'rb') as handle:
            data = handle.read()
    except OSError as exc:
        return OcrOutcome(error=f'read_failed:{type(exc).__name__}')
    return ocr_image_bytes(data, mime)
