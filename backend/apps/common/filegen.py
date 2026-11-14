"""统一文件生成服务（docx / pdf / xlsx / pptx）

把四个文件生成技能集中封装，供写作、表格、PPT、对话等模块按需调用，
避免各 app 重复实现排版与字体处理。
"""
import base64
import io
import logging
import os
import re
import struct
from pathlib import Path
from typing import List, Optional

from django.utils.html import escape

logger = logging.getLogger(__name__)

# ─────────────────────── 依赖与字体 ───────────────────────

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, PageBreak,
    )

    HAS_REPORTLAB = True
except ImportError as _reportlab_err:  # pragma: no cover
    HAS_REPORTLAB = False
    logger.warning('reportlab 未安装，PDF 导出将不可用：%s', _reportlab_err)

_CHINESE_FONT_NAME = 'ChineseFont'
_FONT_REGISTERED = False


def _find_chinese_font_path() -> Optional[str]:
    """按常见路径查找中文字体文件"""
    windir = os.environ.get('WINDIR', 'C:/Windows')
    candidates = [
        # Windows
        Path(windir) / 'Fonts' / 'msyh.ttc',
        Path(windir) / 'Fonts' / 'msyhbd.ttc',
        Path(windir) / 'Fonts' / 'simhei.ttf',
        Path(windir) / 'Fonts' / 'simsun.ttc',
        Path(windir) / 'Fonts' / 'simkai.ttf',
        # Linux 常见发行版
        Path('/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc'),
        Path('/usr/share/fonts/truetype/wqy/wqy-microhei.ttc'),
        Path('/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc'),
        Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
        Path('/usr/share/fonts/truetype/noto/NotoSansCJKsc-Regular.otf'),
        Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return None


def _register_font() -> str:
    """注册中文字体，返回可用字体名"""
    global _FONT_REGISTERED
    if _FONT_REGISTERED:
        return _CHINESE_FONT_NAME

    if not HAS_REPORTLAB:
        return 'Helvetica'

    font_path = _find_chinese_font_path()
    if font_path:
        try:
            # .ttc 是字体集合，需要指定子字体索引
            subfont_index = 0 if font_path.lower().endswith('.ttc') else None
            font = TTFont(_CHINESE_FONT_NAME, font_path, subfontIndex=subfont_index)
            pdfmetrics.registerFont(font)
            _FONT_REGISTERED = True
            logger.info('PDF 中文字体已注册：%s', font_path)
            return _CHINESE_FONT_NAME
        except Exception as e:
            logger.warning('注册中文字体失败 %s：%s', font_path, e)

    # 兜底：用 Helvetica，但中文会变成方框
    logger.warning('未找到可用中文字体，PDF 中文可能显示为方框')
    return 'Helvetica'


def _pdf_base_styles():
    """返回 PDF 常用 ParagraphStyle"""
    font_name = _register_font()
    styles = getSampleStyleSheet()
    styles['Normal'].fontName = font_name
    styles['Normal'].fontSize = 12
    styles['Normal'].leading = 20
    styles['Normal'].firstLineIndent = 24  # 首行缩进约 2 字符

    styles['Heading1'].fontName = font_name
    styles['Heading1'].fontSize = 18
    styles['Heading1'].leading = 26
    styles['Heading1'].alignment = 1  # 居中
    styles['Heading1'].spaceAfter = 16

    styles['Heading2'].fontName = font_name
    styles['Heading2'].fontSize = 14
    styles['Heading2'].leading = 22
    styles['Heading2'].spaceAfter = 10

    styles['Heading3'].fontName = font_name
    styles['Heading3'].fontSize = 12
    styles['Heading3'].leading = 20
    styles['Heading3'].spaceAfter = 6

    return styles


def _split_paragraphs(text: str) -> List[str]:
    """按空行拆分段落"""
    return [p.strip() for p in (text or '').split('\n\n') if p.strip()]


# ─────────────────────── 通用 PDF ───────────────────────

def build_pdf(title: str, content: str) -> bytes:
    """文本内容 → PDF 文件字节（A4，中文自适应字体）"""
    if not HAS_REPORTLAB:
        raise ImportError('PDF 导出依赖 reportlab，请先安装：pip install reportlab')

    styles = _pdf_base_styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        rightMargin=2 * cm,
        leftMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )

    story = []
    if title:
        story.append(Paragraph(escape(title), styles['Heading1']))
        story.append(Spacer(1, 0.3 * cm))

    for para in _split_paragraphs(content or ''):
        # 对 markdown 标题做简单样式映射
        stripped = para.lstrip()
        if stripped.startswith('### '):
            story.append(Paragraph(escape(stripped[4:]), styles['Heading3']))
        elif stripped.startswith('## '):
            story.append(Paragraph(escape(stripped[3:]), styles['Heading2']))
        elif stripped.startswith('# '):
            story.append(Paragraph(escape(stripped[2:]), styles['Heading1']))
        else:
            story.append(Paragraph(escape(para), styles['Normal']))
        story.append(Spacer(1, 0.2 * cm))

    doc.build(story)
    return buf.getvalue()


# ─────────────────────── 表格相关 PDF / DOCX ───────────────────────

_CHART_IMAGE_MAX_BYTES = 8 * 1024 * 1024


def _decode_chart_image(chart_image) -> Optional[bytes]:
    """base64 图表图片（可带 data URL 前缀）→ PNG/JPEG 字节；无效数据抛 ValueError"""
    if not chart_image:
        return None
    if isinstance(chart_image, bytes):
        raw = chart_image
    else:
        text = str(chart_image)
        if ',' in text[:64] and text.lstrip().startswith('data:'):
            text = text.split(',', 1)[1]
        try:
            raw = base64.b64decode(text, validate=True)
        except Exception:
            raise ValueError('图表图片数据无效：不是合法的 base64 图片')
    if len(raw) > _CHART_IMAGE_MAX_BYTES:
        raise ValueError('图表图片过大（超过 8MB）')
    if not (raw[:8].startswith(b'\x89PNG\r\n\x1a\n') or raw[:2] == b'\xff\xd8'):
        raise ValueError('图表图片仅支持 PNG / JPEG 格式')
    return raw


def _png_size(png_bytes: bytes):
    """PNG IHDR 宽高；非 PNG 返回 None"""
    if png_bytes[:8].startswith(b'\x89PNG\r\n\x1a\n') and png_bytes[12:16] == b'IHDR':
        w, h = struct.unpack('>II', png_bytes[16:24])
        return w, h
    return None


def table_to_pdf(name: str, data: List[List], chart_image=None) -> bytes:
    """二维表格 → PDF 文件字节；chart_image 为 base64 图片时嵌在表格上方"""
    if not HAS_REPORTLAB:
        raise ImportError('PDF 导出依赖 reportlab，请先安装：pip install reportlab')

    styles = _pdf_base_styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
    )

    story = []
    if name:
        story.append(Paragraph(escape(name), styles['Heading1']))
        story.append(Spacer(1, 0.4 * cm))

    img = _decode_chart_image(chart_image)
    if img:
        usable = A4[0] - 3 * cm
        max_h = 9 * cm
        size = _png_size(img)
        if size and size[0] > 0 and size[1] > 0:
            scale = min(usable / size[0], max_h / size[1], 1.0)
            w, h = size[0] * scale, size[1] * scale
        else:
            w, h = usable, max_h
        story.append(Image(io.BytesIO(img), width=w, height=h))
        story.append(Spacer(1, 0.4 * cm))

    if not data:
        doc.build(story)
        return buf.getvalue()

    # 全部转字符串
    table_data = [[escape(str(cell)) for cell in row] for row in data]
    col_count = max(len(row) for row in table_data) if table_data else 0

    # 列宽：按可用宽度均分，最小 1.5cm
    usable_width = A4[0] - 3 * cm
    col_width = max(usable_width / max(col_count, 1), 1.5 * cm)
    col_widths = [col_width] * col_count

    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    style_commands = [
        ('FONTNAME', (0, 0), (-1, -1), _register_font()),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]
    if table_data:
        style_commands.append(
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1F4E79'))
        )
        style_commands.append(
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white)
        )
        style_commands.append(
            ('FONTNAME', (0, 0), (-1, 0), _register_font())
        )

    table.setStyle(TableStyle(style_commands))
    story.append(table)
    doc.build(story)
    return buf.getvalue()


def table_to_docx(name: str, data: List[List], chart_image=None) -> bytes:
    """二维表格 → Word 文件字节（简单表格样式）；chart_image 为 base64 图片时嵌在表格上方"""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor

    doc = Document()
    style = doc.styles['Normal']
    style.font.name = '微软雅黑'
    style.font.size = Pt(11)
    style._element.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')

    if name:
        heading = doc.add_heading(name, level=1)
        heading.alignment = WD_ALIGN_PARAGRAPH.CENTER

    img = _decode_chart_image(chart_image)
    if img:
        doc.add_picture(io.BytesIO(img), width=Inches(6.3))
        doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER

    if not data:
        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()

    table = doc.add_table(rows=len(data), cols=max(len(row) for row in data))
    table.style = 'Table Grid'

    for i, row in enumerate(data):
        for j, cell_value in enumerate(row):
            cell = table.cell(i, j)
            cell.text = str(cell_value)
            for paragraph in cell.paragraphs:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                for run in paragraph.runs:
                    run.font.name = '微软雅黑'
                    run.font.size = Pt(10)
                    run._element.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')
                    if i == 0:
                        run.font.bold = True
                        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

    # 表头底色
    for cell in table.rows[0].cells:
        tcPr = cell._tc.get_or_add_tcPr()
        shd = tcPr.find(qn('w:shd'))
        if shd is None:
            from docx.oxml import OxmlElement
            shd = OxmlElement('w:shd')
            tcPr.append(shd)
        shd.set(qn('w:fill'), '1F4E79')

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ─────────────────────── PPT 相关 PDF / DOCX ───────────────────────

def ppt_to_pdf(title: str, slides: List[dict]) -> bytes:
    """PPT slides JSON → PDF 文件字节（每页一页）"""
    if not HAS_REPORTLAB:
        raise ImportError('PDF 导出依赖 reportlab，请先安装：pip install reportlab')

    styles = _pdf_base_styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        rightMargin=2 * cm,
        leftMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )

    story = []
    if title:
        story.append(Paragraph(escape(title), styles['Heading1']))
        story.append(PageBreak())

    for idx, slide in enumerate(slides or []):
        if idx > 0:
            story.append(PageBreak())
        slide_title = slide.get('title', '')
        if slide_title:
            story.append(Paragraph(escape(slide_title), styles['Heading2']))
        bullets = slide.get('bullets') or []
        for bullet in bullets:
            story.append(Paragraph(f'• {escape(str(bullet))}', styles['Normal']))
            story.append(Spacer(1, 0.15 * cm))
        notes = slide.get('notes', '')
        if notes:
            story.append(Spacer(1, 0.3 * cm))
            story.append(Paragraph(f'备注：{escape(notes)}', styles['Normal']))

    doc.build(story)
    return buf.getvalue()


def ppt_to_docx(title: str, slides: List[dict]) -> bytes:
    """PPT slides JSON → Word 文件字节"""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Pt

    doc = Document()
    style = doc.styles['Normal']
    style.font.name = '微软雅黑'
    style.font.size = Pt(11)
    style._element.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')

    if title:
        heading = doc.add_heading(title, level=1)
        heading.alignment = WD_ALIGN_PARAGRAPH.CENTER

    for slide in slides or []:
        doc.add_heading(slide.get('title', ''), level=2)
        for bullet in slide.get('bullets') or []:
            p = doc.add_paragraph(style='List Bullet')
            p.add_run(str(bullet)).font.name = '微软雅黑'
            p.runs[0]._element.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')
        notes = slide.get('notes', '')
        if notes:
            p = doc.add_paragraph()
            run = p.add_run(f'备注：{notes}')
            run.font.name = '微软雅黑'
            run.font.size = Pt(10)
            run.font.color.rgb = None  # 默认
            run._element.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')
        doc.add_paragraph()

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ─────────────────────── docx / xlsx / pptx 生成 ───────────────────────

_MD_BOLD = re.compile(r'\*\*(.+?)\*\*')


def _clean_md_inline(text: str) -> str:
    """去掉行内 markdown 强调标记，保留文字"""
    text = _MD_BOLD.sub(r'\1', text)
    return text.replace('`', '')


def build_docx(title: str, content: str) -> bytes:
    """Markdown 风格纯文本 → docx 字节（# 标题 / - 列表 / 其余正文）"""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Pt

    doc = Document()
    normal = doc.styles['Normal']
    normal.font.name = '微软雅黑'
    normal.font.size = Pt(11)
    normal._element.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')

    if title:
        heading = doc.add_heading(title, level=1)
        heading.alignment = WD_ALIGN_PARAGRAPH.CENTER

    for raw in (content or '').splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('###'):
            doc.add_heading(_clean_md_inline(stripped.lstrip('#').strip()), level=3)
        elif stripped.startswith('##'):
            doc.add_heading(_clean_md_inline(stripped.lstrip('#').strip()), level=2)
        elif stripped.startswith('#'):
            doc.add_heading(_clean_md_inline(stripped.lstrip('#').strip()), level=2)
        elif stripped[:2] in ('- ', '* '):
            p = doc.add_paragraph(style='List Bullet')
            run = p.add_run(_clean_md_inline(stripped[2:]))
            run.font.name = '微软雅黑'
            run._element.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')
        else:
            p = doc.add_paragraph()
            run = p.add_run(_clean_md_inline(stripped))
            run.font.name = '微软雅黑'
            run._element.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def build_xlsx(name: str, data: List[List]) -> bytes:
    """二维表格 → xlsx 字节，首行加粗并冻结"""
    import openpyxl
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = (name or 'Sheet1')[:31]

    for r, row in enumerate(data or [], 1):
        for c, value in enumerate(row or [], 1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.alignment = Alignment(vertical='top', wrap_text=True)
            if r == 1:
                cell.font = Font(bold=True)

    for c in range(1, max((len(r) for r in (data or [[]])), default=1) + 1):
        width = 12
        for r in range(1, len(data or []) + 1):
            row = data[r - 1]
            if c <= len(row) and row[c - 1] is not None:
                width = max(width, min(60, len(str(row[c - 1])) + 4))
        ws.column_dimensions[get_column_letter(c)].width = width

    if data:
        ws.freeze_panes = 'A2'

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_pptx(title: str, slides: List[dict], subtitle: str = '') -> bytes:
    """幻灯片列表 → pptx 字节（每项含 title / bullets / notes）"""
    from pptx import Presentation

    prs = Presentation()
    first = prs.slides.add_slide(prs.slide_layouts[0])
    first.shapes.title.text = title or '演示文稿'
    if subtitle and len(first.placeholders) > 1:
        first.placeholders[1].text = subtitle

    body_layout = prs.slide_layouts[1]
    for slide in slides or []:
        s = prs.slides.add_slide(body_layout)
        s.shapes.title.text = slide.get('title', '')
        body = s.placeholders[1].text_frame
        bullets = slide.get('bullets') or []
        for i, bullet in enumerate(bullets):
            para = body.paragraphs[0] if i == 0 else body.add_paragraph()
            para.text = str(bullet)
        notes = slide.get('notes', '')
        if notes:
            s.notes_slide.notes_text_frame.text = str(notes)

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()
