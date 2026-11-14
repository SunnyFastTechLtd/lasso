"""对话导出服务"""
from datetime import datetime
from typing import Literal

from backend.apps.common import filegen


ExportType = Literal['md', 'docx', 'pdf']

# P1-c：会话内切换助手总开关。出厂关闭，关闭时切换入口不可用，
# 发送消息仍按原有的 agent_locked 闸门处理，存量行为完全不变
AGENT_SWITCH_KEY = 'chat_agent_switch_enabled'


def agent_switch_enabled() -> bool:
    """读取「会话内切换助手」功能开关（默认关闭）"""
    from backend.apps.settings.models import SystemSetting
    raw = SystemSetting.get_value(AGENT_SWITCH_KEY, 'false')
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


# E3 追问补全：出现这些指代/省略信号时视为可能的追问
_FOLLOWUP_HINTS = ('它', '他', '她', '这个', '那个', '该', '其', '上面', '刚才',
                   '还有呢', '然后呢', '继续')


def needs_rewrite(question: str, history) -> bool:
    """E3：判断本轮问题是否需要按上文补全后再检索

    只在有历史、且问题带指代/省略信号时触发，避免每轮都多付一次改写调用。
    """
    text = (question or '').strip()
    if not text or not history:
        return False
    return any(hint in text for hint in _FOLLOWUP_HINTS)


def standalone_question(question: str, history) -> str:
    """E3：把省略式追问补成可独立检索的完整问题

    不暴露所用模型与参数；改写失败时原样返回，绝不阻断对话。
    """
    from backend.apps.common.llm import chat_completion

    recent = history[-6:]
    transcript = '\n'.join(
        f"{'用户' if m.get('role') == 'user' else '助手'}：{(m.get('content') or '')[:200]}"
        for m in recent
    )
    messages = [
        {'role': 'system', 'content':
            '你是检索查询改写器。根据对话历史，把用户最后一句话补全为可独立检索的完整问题。'
            '只输出改写后的问题，不要解释、不要引号；原问题已完整时原样输出。'},
        {'role': 'user', 'content':
            f'对话历史：\n{transcript}\n\n用户最后一句话：{question}'},
    ]
    try:
        text = (chat_completion(messages, temperature=0, max_tokens=128) or '').strip()
    except Exception:
        return question
    return text or question


def _format_time(dt: datetime | None) -> str:
    if dt is None:
        return ''
    return dt.strftime('%H:%M')


def build_export_markdown(conversation) -> str:
    """将会话消息渲染为 Markdown 归档文本"""
    lines = []
    title = conversation.title or '未命名会话'
    lines.append(f'# {title}\n')
    lines.append(f'- 创建时间：{conversation.created_at.strftime("%Y-%m-%d %H:%M")}')
    lines.append(f'- 消息数：{conversation.messages.count()}\n')

    for msg in conversation.messages.all().order_by('created_at'):
        if msg.role == 'user':
            header = f"## 问（{_format_time(msg.created_at)}）"
        elif msg.role == 'assistant':
            header = f"## 答（{_format_time(msg.created_at)}）"
        else:
            continue
        lines.append(header)
        lines.append('')
        lines.append(msg.content.strip())
        if msg.attachments:
            for att in msg.attachments:
                name = att.get('name', '附件')
                lines.append(f'')
                lines.append(f'> 附件：{name}')
        lines.append('')
        lines.append('---')
        lines.append('')

    return '\n'.join(lines).strip() + '\n'


def build_export_text(conversation) -> str:
    """将会话消息渲染为适合 PDF/打印的纯文本"""
    lines = []
    title = conversation.title or '未命名会话'
    lines.append(title)
    lines.append('')
    lines.append(f'创建时间：{conversation.created_at.strftime("%Y-%m-%d %H:%M")}')
    lines.append(f'消息数：{conversation.messages.count()}')
    lines.append('')

    for msg in conversation.messages.all().order_by('created_at'):
        if msg.role == 'user':
            label = f"问（{_format_time(msg.created_at)}）"
        elif msg.role == 'assistant':
            label = f"答（{_format_time(msg.created_at)}）"
        else:
            continue
        lines.append(label)
        lines.append('-' * len(label))
        lines.append(msg.content.strip())
        if msg.attachments:
            for att in msg.attachments:
                name = att.get('name', '附件')
                lines.append(f'[附件：{name}]')
        lines.append('')

    return '\n'.join(lines)


def export_conversation(conversation, export_type: ExportType) -> tuple[bytes, str, str]:
    """导出一个会话为指定格式

    Returns:
        (content_bytes, mime_type, file_extension)
    """
    title = conversation.title or '未命名会话'

    if export_type == 'docx':
        md_text = build_export_markdown(conversation)
        content_bytes = filegen.build_docx(title, md_text)
        return content_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'docx'

    if export_type == 'pdf':
        text = build_export_text(conversation)
        content_bytes = filegen.build_pdf(title, text)
        return content_bytes, 'application/pdf', 'pdf'

    md_text = build_export_markdown(conversation)
    return md_text.encode('utf-8'), 'text/markdown; charset=utf-8', 'md'
