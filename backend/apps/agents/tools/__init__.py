"""
智能体工具集

所有业务工具统一在此定义，供智能体注册中心装配。
工具通过 FunctionTool 注册时必须显式授予 ALLOW 权限，
否则 AgentScope 默认会挂起等待人工确认（human-in-the-loop），
不适合后端自动执行场景。

Django ORM 是同步的，工具在 AgentScope 的异步事件循环中执行，
因此 ORM 调用一律通过 sync_to_async 包装。
"""
import uuid
from pathlib import Path, PurePosixPath
from typing import List, Dict, Any, Optional, Callable

from asgiref.sync import sync_to_async
from agentscope.message import TextBlock
from agentscope.tool import FunctionTool, ToolChunk
from agentscope.permission import PermissionDecision, PermissionBehavior
from django.core.files.base import ContentFile


def _allow_all() -> PermissionDecision:
    """后端自动执行场景：显式授权，跳过人工确认"""
    return PermissionDecision(
        behavior=PermissionBehavior.ALLOW,
        message='系统内置工具，自动授权',
    )


def _ok(text: str, metadata: Dict[str, Any] | None = None) -> ToolChunk:
    return ToolChunk(
        content=[TextBlock(type='text', text=text)],
        state='success',
        is_last=True,
        metadata=metadata or {},
    )


def _fail(text: str) -> ToolChunk:
    return ToolChunk(
        content=[TextBlock(type='text', text=text)],
        state='error',
        is_last=True,
        metadata={},
    )


# ─── 知识库检索工具 ───

# 保留 single_embedding 名字，实际为本地 fastembed
from backend.apps.common.embedding import embed_query as single_embedding
from backend.apps.knowledge.services import search_chunks


def _semantic_search(kb_id: int, question: str, top_k: int = 5) -> List[Dict[str, Any]]:
    """问题向量化 + 语义检索（同步，在 sync_to_async 中执行）"""
    return search_chunks(kb_id, single_embedding(question), top_k=top_k)


async def search_knowledge_base(question: str, knowledge_base_id: int) -> ToolChunk:
    """在知识库中语义检索相关内容

    Args:
        question: 用户问题
        knowledge_base_id: 知识库 ID
    """
    try:
        hits = await sync_to_async(_semantic_search)(knowledge_base_id, question)
    except Exception as e:
        return _fail(f'知识库检索失败：{e}')

    if not hits:
        return _ok('未检索到相关内容，请基于自身知识回答并提示用户知识库内容不足。')

    parts = []
    for i, hit in enumerate(hits, 1):
        parts.append(
            f"【片段{i}】（来源：《{hit['doc_title']}》{hit['file_name']}，相似度 {hit['score']:.2f}）\n{hit['content']}"
        )
    return _ok(
        '\n\n'.join(parts),
        metadata={'sources': [
            {'doc_title': h['doc_title'], 'file_name': h['file_name'], 'score': h['score']}
            for h in hits
        ]},
    )


def build_knowledge_tools(knowledge_base_id: int, user_id: Optional[int] = None) -> List[FunctionTool]:
    """为指定知识库构建检索工具集"""
    async def search(question: str) -> ToolChunk:
        """在当前知识库中语义检索相关内容

        Args:
            question: 用户问题
        """
        return await search_knowledge_base(question, knowledge_base_id)

    return [
        FunctionTool(
            search,
            name='search_knowledge_base',
            description='在知识库中检索与用户问题相关的文档内容，回答知识库问题前应先调用',
            permission=_allow_all(),
        ),
    ]


# ─── 多知识库检索工具（对话中勾选知识库） ───

def _kb_names(kb_ids: List[int]) -> Dict[int, str]:
    """查询知识库名称（同步，在 sync_to_async 中执行）"""
    from backend.apps.knowledge.models import KnowledgeBase

    return {kb.id: kb.name for kb in KnowledgeBase.objects.filter(id__in=kb_ids)}


def multi_kb_search(
    kb_ids: List[int], question: str, top_k: int = 5
) -> List[Dict[str, Any]]:
    """跨多个知识库检索：同一查询向量在各库分别检索，按相似度归并取前 top_k

    同步函数，供工具与视图（取引用来源）复用，须在 sync_to_async 中调用。
    """
    vector = single_embedding(question)
    names = _kb_names(kb_ids)
    merged: List[Dict[str, Any]] = []
    for kb_id in kb_ids:
        for hit in search_chunks(kb_id, vector, top_k=top_k):
            hit['kb_id'] = kb_id
            hit['kb_name'] = names.get(kb_id, '')
            merged.append(hit)
    merged.sort(key=lambda h: h['score'], reverse=True)
    return merged[:top_k]


def build_multi_knowledge_tools(
    knowledge_base_ids: List[int], user_id: Optional[int] = None,
    source_sink: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
) -> List[FunctionTool]:
    """为对话中勾选的多个知识库构建跨库检索工具集

    source_sink：E3 按需查下用于收集真实检索命中，供会话落引用来源。
    """
    kb_ids = list(knowledge_base_ids)

    async def search(question: str) -> ToolChunk:
        """在用户勾选的知识库中语义检索相关内容

        Args:
            question: 用户问题
        """
        try:
            hits = await sync_to_async(multi_kb_search)(kb_ids, question)
        except Exception as e:
            return _fail(f'知识库检索失败：{e}')

        if source_sink is not None and hits:
            try:
                source_sink(hits)
            except Exception:
                pass

        if not hits:
            return _ok('所选知识库中未检索到相关内容，请明确告知用户知识库暂无相关资料。')

        parts = []
        for i, hit in enumerate(hits, 1):
            parts.append(
                f"【片段{i}】（知识库：{hit['kb_name']}，来源：《{hit['doc_title']}》"
                f"{hit['file_name']}，相似度 {hit['score']:.2f}）\n{hit['content']}"
            )
        return _ok(
            '\n\n'.join(parts),
            metadata={'sources': [
                {
                    'knowledge_base_id': h['kb_id'],
                    'knowledge_base_name': h['kb_name'],
                    'doc_title': h['doc_title'],
                    'file_name': h['file_name'],
                    'score': h['score'],
                }
                for h in hits
            ]},
        )

    return [
        FunctionTool(
            search,
            name='search_knowledge_base',
            description='在用户勾选的知识库中检索与问题相关的文档内容，'
                        '回答资料性问题前应先调用，并基于检索结果注明来源',
            permission=_allow_all(),
        ),
    ]


# ─── 技能包附件只读工具（C1） ───

# 单次读取上限：附件本身最大 200KB，但一次塞进上下文会挤掉对话预算
SKILL_FILE_READ_LIMIT = 120 * 1024


def build_skill_file_tools(skill_dirs: Dict[str, str]) -> List[FunctionTool]:
    """为本次装配的技能包构建 read_skill_file 只读工具。

    只允许读取本次运行目录里的文件：skill_dirs 由 plan_skills 产出（已按
    C5 生效范围过滤），工具不接受任意路径，因此不会读到未挂载技能或系统文件。
    """
    roots = {slug: Path(directory).resolve() for slug, directory in (skill_dirs or {}).items()}
    if not roots:
        return []

    async def read_skill_file(skill: str, path: str) -> ToolChunk:
        """读取已挂载技能包中的附属文件

        当技能规范要求查阅附件（如 references/、examples/ 下的文件）时调用。
        只能读取当前挂载技能包内的文本文件。

        Args:
            skill: 技能标识（技能目录名/slug）
            path: 技能包内的相对路径，如 references/faq.md
        """
        slug = (skill or '').strip()
        root = roots.get(slug)
        if root is None:
            return _fail(f'未挂载技能「{slug}」，可用技能：{"、".join(sorted(roots))}')

        rel = str(path or '').replace('\\', '/').strip()
        if not rel:
            return _fail('文件路径不能为空')
        pure = PurePosixPath(rel)
        if pure.is_absolute() or any(part in ('', '.', '..') for part in pure.parts):
            return _fail('文件路径不合法（不允许绝对路径或 ..）')
        if any(part.startswith('.') for part in pure.parts):
            return _fail('不允许读取隐藏文件')
        target = (root / pure).resolve()
        if root not in target.parents:
            return _fail('文件路径超出技能包范围')
        if not target.is_file():
            return _fail(f'技能包内不存在文件：{rel}')

        try:
            raw = target.read_bytes()
        except OSError as exc:
            return _fail(f'读取失败：{exc}')
        truncated = len(raw) > SKILL_FILE_READ_LIMIT
        raw = raw[:SKILL_FILE_READ_LIMIT]
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            return _fail('该文件不是 UTF-8 文本，无法读取')

        suffix = '\n\n…（文件较大，已截断，仅返回前 120KB）' if truncated else ''
        return _ok(
            f'【{slug}/{rel}】\n{text}{suffix}',
            metadata={'skill': slug, 'path': rel, 'size': target.stat().st_size,
                      'truncated': truncated},
        )

    return [
        FunctionTool(
            read_skill_file,
            name='read_skill_file',
            description='读取已挂载技能包中的附属文件（如 references/xxx.md）。'
                        '当技能规范要求查阅附件时调用；先按技能规范作答，不要臆测附件内容。'
                        '参数：skill(技能标识/slug), path(包内相对路径)。',
            permission=_allow_all(),
        ),
    ]


# ─── 对话文件生成工具 ───

# 允许生成的文件类型与对应 MIME
def _mime_for_type(file_type: str) -> str:
    return {
        'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'pdf': 'application/pdf',
    }.get(file_type, 'application/octet-stream')


async def generate_file(
    file_type: str,
    title: str,
    content: str = '',
    data: Optional[List[List]] = None,
    slides: Optional[List[Dict[str, Any]]] = None,
    subtitle: str = '',
    user_id: int = 0,
    conversation_id: Optional[int] = None,
) -> ToolChunk:
    """在对话中生成可下载文件（docx / xlsx / pptx / pdf）

    当用户明确要求生成 Word、Excel、PPT 或 PDF 文件时调用。
    生成成功后返回文件下载引用，最终回复中应提示用户已在对话中生成文件。

    Args:
        file_type: 文件类型，必须是 docx / xlsx / pptx / pdf
        title: 文件标题
        content: 文档正文（docx / pdf 使用）
        data: 二维表格数据（xlsx 使用）
        slides: 幻灯片列表（pptx 使用），每项含 title / bullets / notes
        subtitle: PPT 副标题（pptx 使用）
        user_id: 当前用户 ID（由系统注入，不暴露给模型）
        conversation_id: 当前会话 ID（由系统注入，不暴露给模型）
    """
    from backend.apps.chat.models import ChatGeneratedFile
    from backend.apps.common import filegen

    file_type = (file_type or '').lower().lstrip('.')
    if file_type not in ('docx', 'xlsx', 'pptx', 'pdf'):
        return _fail('不支持的文件类型，仅支持 docx / xlsx / pptx / pdf')

    safe_title = (title or '未命名').replace('/', '_').replace('\\', '_').strip()
    file_name = f'{safe_title}.{file_type}'

    try:
        if file_type == 'docx':
            file_bytes = filegen.build_docx(safe_title, content or '')
        elif file_type == 'pdf':
            file_bytes = filegen.build_pdf(safe_title, content or '')
        elif file_type == 'xlsx':
            file_bytes = filegen.build_xlsx(safe_title, data or [])
        else:  # pptx
            file_bytes = filegen.build_pptx(safe_title, slides or [], subtitle=subtitle)
    except Exception as e:
        return _fail(f'文件生成失败：{e}')

    try:
        # thread_sensitive=False 避免在 AgentScope 后台事件循环中执行时
        # 与 Django TestCase 的共享 SQLite 连接产生表锁冲突
        record = await sync_to_async(
            ChatGeneratedFile.objects.create,
            thread_sensitive=False,
        )(
            owner_id=user_id,
            conversation_id=conversation_id,
            file_name=file_name,
            file_type=file_type,
            file_size=len(file_bytes),
            file=ContentFile(file_bytes, name=f'{uuid.uuid4().hex}.{file_type}'),
        )
    except Exception as e:
        return _fail(f'文件保存失败：{e}')

    return _ok(
        f'已生成文件《{file_name}》[GENERATED_FILE:{record.id}]，请在对话中查看并下载。',
        metadata={
            'file_id': record.id,
            'file_name': file_name,
            'file_type': file_type,
            'file_size': len(file_bytes),
        },
    )


def build_chat_tools(
    user_id: int,
    resource_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
) -> List[FunctionTool]:
    """为通用对话智能体构建平台内置工具集（文件生成）"""
    async def create_file(
        file_type: str,
        title: str,
        content: str = '',
        data: Optional[List[List]] = None,
        slides: Optional[List[Dict[str, Any]]] = None,
        subtitle: str = '',
    ) -> ToolChunk:
        """生成可下载文件（docx / xlsx / pptx / pdf）"""
        return await generate_file(
            file_type=file_type,
            title=title,
            content=content,
            data=data,
            slides=slides,
            subtitle=subtitle,
            user_id=user_id,
            conversation_id=conversation_id,
        )

    return [
        FunctionTool(
            create_file,
            name='generate_file',
            description='当用户明确要求生成 Word(docx)、Excel(xlsx)、PPT(pptx) 或 PDF 文件时调用。'
                        '参数：file_type(文件类型), title(标题), content(docx/pdf 的正文内容), '
                        'data(xlsx 的二维表格数据), slides(pptx 的幻灯片列表，每项含 title/bullets/notes), subtitle(pptx 副标题)。'
                        '调用成功后会返回文件引用，最终回复中应提示用户文件已生成。',
            permission=_allow_all(),
        ),
    ]
