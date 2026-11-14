"""运行时装配（PRD 第 6 章 D 组 D1–D6）

把「智能体定义（AgentProfile）」变成一次真正可执行的 AgentScope 会话：
模型 + 说话方式 + 提示词 + MCP 工具来源 + 技能 + 知识库 + 授权档位，
全部取值来自 agenthub.services.runtime_config() 的装配快照，档位→数值
的换算已在服务层完成，本模块只负责"按快照装配"和"装配不了时怎么办"。

三条硬口径：
1. 唯一键：平台侧一律用 mcp__{来源slug}__{工具名}（7.1）。AgentScope 会把
   MCP 工具名规范化成 ^[a-zA-Z0-9_-]+$（非法字符替换为 x，见 tool/_adapters.py），
   授权规则必须用规范化后的名字，enable_tools 必须用来源返回的原始名。
2. 快照优先：进行中与已发布的会话用线上版本快照（7.3），未发布过的草稿
   用当前配置，避免"改了草稿就影响正在跑的会话"。
3. 降级不阻断（D6）：来源停用、出网未开放、工具失效、技能未生效、知识库越权
   一律跳过该来源并产出结构化降级说明，绝不静默消失，也绝不让整轮对话失败；
   说明由 SSE 以降级条下发，界面给出可点的重试。

授权档位（A5/D7）：白名单 = PermissionMode.DEFAULT + allow_rules（勾选的工具）
+ deny_rules（已挂载但未勾选的工具）；自动放行 = BYPASS。逐次询问 = DEFAULT
+ 白名单 ALLOW + 其余工具显式 ASK（只读工具也会询问），由会话内授权卡片逐次
决定；非交互调用（无授权入口）回退为白名单语义并给出显式降级说明，不静默
挂起。

MCP 生命周期：Toolkit 只校验 stateful 客户端已连接、不负责连接（_toolkit.py
对 is_stateful 且未 connect 的客户端直接报错）。因此 stdio 客户端的连接与关闭
必须落在与 reply_stream 同一个事件循环里，统一由 bridge 的装配执行入口管理。

命令型（stdio）来源在 P0 不注入会话（PRD #8 已决）：平台常驻托管未随版本交付，
每次调用独立拉起子进程在并发与状态语义上都不可控，因此会话侧一律跳过并给降级
说明，命令型来源在 P0 只支持注册与连通性测试；业务调用请改用接口型来源。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)

# 知识库检索后的作答要求（装配了检索工具的会话统一追加）
# E3 按需查：是否检索由模型判断，闲聊与纯格式改写不查库
KB_PROMPT_SUFFIX = (
    '\n\n你可以根据用户问题调用 search_knowledge_base 工具检索知识库内容，'
    '并基于检索结果回答，引用时注明来源文档名称。'
    '闲聊、寒暄与纯格式改写不需要检索；涉及资料、制度、流程的问题应先检索。'
)

# E3 每轮必查：强制先检索再作答（与"无命中不得改用常识作答"同向）
KB_PROMPT_ALWAYS = (
    '\n\n回答任何涉及资料、制度、流程的问题前，必须先调用 search_knowledge_base '
    '检索知识库，并只依据检索结果作答；检索无命中时明确告知用户知识库暂无相关资料，'
    '不得改用常识作答。引用时注明来源文档名称。'
)

# E3 追问补全：省略式追问先补成完整问题再检索
KB_PROMPT_FOLLOWUP = (
    '\n\n用户问题若是省略式追问（如"那它的价格呢"），'
    '先结合上文补全为可独立检索的完整问题，再调用 search_knowledge_base。'
)

# 内置文件生成能力的作答要求
FILE_PROMPT_SUFFIX = (
    '\n\n当用户明确要求生成 Word / Excel / PPT / PDF 文件时，'
    '调用 generate_file 工具生成，并在回复中说明文件已生成可供下载。'
)

# 技能包附属文件的读取口径（C1）
SKILL_PACKAGE_PROMPT_SUFFIX = (
    '\n\n挂载的技能包可能附带参考文件。先按技能规范作答；'
    '当规范要求查阅附件时，调用 read_skill_file(skill=技能标识, path=包内相对路径) 读取，'
    '不要臆测附件内容。'
)

_PLACEHOLDER_RE = re.compile(r'\{([a-z_]+)\}')
# AgentScope 对 MCP 工具名施加的字符集约束（非法字符按官方口径替换为 x）
_ASL_ILLEGAL_RE = re.compile(r'[^a-zA-Z0-9_-]')


@dataclass
class Degrade:
    """一条降级说明：哪个来源、为什么没生效、用户或管理员该怎么重试"""

    kind: str
    name: str
    reason: str
    suggestion: str = ''

    def as_dict(self) -> Dict[str, str]:
        return {
            'kind': self.kind,
            'name': self.name,
            'reason': self.reason,
            'suggestion': self.suggestion,
        }


@dataclass
class MCPPlan:
    """一个可以注入本次会话的 MCP 来源"""

    slug: str
    name: str
    transport: str
    stateful: bool
    url: str = ''
    headers: Dict[str, str] = field(default_factory=dict)
    command: str = ''
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: str = ''
    timeout: int = 30
    tool_names: List[str] = field(default_factory=list)
    full_names: List[str] = field(default_factory=list)
    asl_names: List[str] = field(default_factory=list)
    # F4：该来源走外网出口，其工具调用计入「出网调用次数」而非内网工具调用
    requires_egress: bool = False

    @property
    def client_name(self) -> str:
        return _ASL_ILLEGAL_RE.sub('x', self.slug)

    def display_of(self, asl_name: str) -> str:
        return f'{self.name}·{asl_name.rsplit("__", 1)[-1]}'


@dataclass
class PermissionPlan:
    mode: Any = None
    allow_rules: Dict[str, list] = field(default_factory=dict)
    ask_rules: Dict[str, list] = field(default_factory=dict)
    deny_rules: Dict[str, list] = field(default_factory=dict)


@dataclass
class Assembled:
    """装配结果：Agent + 需要在会话结束后关闭的客户端 + 过程展示口径"""

    agent: Any
    notes: List[Degrade] = field(default_factory=list)
    clients: List[Any] = field(default_factory=list)
    displays: Dict[str, str] = field(default_factory=dict)
    # F4：走外网出口的工具（AgentScope 工具名口径），用于互斥归属出网调用
    egress_tools: set = field(default_factory=set)
    # B8-b1：出网工具对应的目标域名，供过程区显示"参数摘要发给了谁"
    egress_hosts: Dict[str, str] = field(default_factory=dict)

    async def aclose(self) -> None:
        for client in self.clients:
            try:
                await client.close()
            except Exception as exc:  # 收尾失败只影响本会话，不能冒泡打断 SSE
                logger.warning('MCP 客户端 %s 关闭失败：%s', client.name, exc)


def asl_tool_name(slug: str, raw_name: str) -> str:
    """平台唯一键 → AgentScope 实际工具名（MCP 适配器同款规范化）"""
    return f'mcp__{_ASL_ILLEGAL_RE.sub("x", slug)}__{_ASL_ILLEGAL_RE.sub("x", raw_name)}'


def _sync(func):
    """同步 ORM 调用切线程池：装配在 reply_stream 所在事件循环中执行"""
    return sync_to_async(func, thread_sensitive=False)


# ─── 取值与提示词 ───

def resolve_snapshot(agent_id: int, user, draft: bool = False,
                     agent_version: int | None = None) -> Tuple[Optional[dict], Any, List[Degrade]]:
    """按 agent_id 取装配快照（同步，须在 sync_to_async 中调用）

    返回 (快照, AgentProfile, 降级说明)。取不到时返回 (None, agent 或 None, 说明)，
    由调用方决定是回落到通用对话还是直接报错。

    draft=True（A3 试聊）固定取草稿快照：即使已有线上版本，也按最新保存的
    未发布配置装配，让管理员不用发布就能验证改动。
    agent_version（7.3）：进行中会话沿用开场快照，回滚/重发布只影响之后的会话。
    """
    from backend.apps.agenthub.models import AgentProfile, AgentVersion
    from backend.apps.agenthub.services import (
        can_see, model_available, runtime_config,
    )
    from backend.apps.common.llm import get_llm_config

    notes: List[Degrade] = []
    agent = AgentProfile.objects.filter(pk=agent_id).first()
    if agent is None:
        return None, None, [Degrade('agent', f'#{agent_id}', '智能体不存在或已被删除')]

    if user is not None and not can_see(user, agent):
        return None, agent, [Degrade('agent', agent.name, '你没有使用该智能体的权限')]

    version = None
    if agent.current_version and not draft:
        versions = AgentVersion.objects.filter(agent=agent)
        if agent_version:
            version = versions.filter(version=agent_version).first()
        if version is None:
            version = versions.order_by('-version').first()
    cfg = runtime_config(agent, version)

    default_model = get_llm_config('llm_model') or ''
    if not cfg.get('model'):
        if default_model:
            notes.append(Degrade('model', '未指定', '智能体未选择模型，已改用平台默认接入'))
        else:
            notes.append(Degrade(
                'model', '未指定',
                '智能体未选择模型，且平台尚未配置默认模型',
                '请到「系统设置 → 模型接入与凭据」完成配置',
            ))
        cfg['model'] = default_model
    elif not model_available(cfg['model']):
        notes.append(Degrade(
            'model', cfg['model'],
            '该模型接入未通过健康检查，已改用平台默认接入' if default_model else '该模型接入未通过健康检查，且平台尚未配置默认模型',
            '在系统设置中确认模型服务可用后重试',
        ))
        cfg['model'] = default_model

    return cfg, agent, notes


def render_prompt(cfg: dict, user) -> str:
    """A3 提示词占位符替换 + 能力追加说明"""
    from backend.apps.agenthub.services import PROMPT_PLACEHOLDERS

    prompt = cfg.get('system_prompt') or ''
    values = {
        'current_date': date.today().isoformat(),
        'user_name': getattr(user, 'display_name', '') or getattr(user, 'username', ''),
        'department': getattr(user, 'department', '') or '',
    }
    for token in PROMPT_PLACEHOLDERS:
        key = token['token'].strip('{}')
        prompt = prompt.replace(token['token'], values.get(key, ''))
    # 管理员手写的未知占位符不猜语义，原样去掉花括号避免喂给模型当模板
    prompt = _PLACEHOLDER_RE.sub(lambda m: m.group(1), prompt)
    return prompt.strip() or '你是一个企业智能体助手。'


# ─── 能力规划（全部同步，返回可注入对象 + 降级说明） ───

def plan_mcp(cfg: dict) -> Tuple[List[MCPPlan], List[Degrade]]:
    """D4/D5/D6：逐个来源逐个工具判定能否注入，不能的必须说明原因"""
    from backend.apps.agenthub.services import tool_full_name
    from backend.apps.mcp.models import MCPServer
    from backend.apps.mcp.services import egress_allowed

    plans: List[MCPPlan] = []
    notes: List[Degrade] = []
    allow = set(cfg.get('allow_tool_names') or [])

    for mount in cfg.get('mcp_servers') or []:
        slug = mount.get('server') or ''
        chosen = list(mount.get('tool_names') or [])
        server = MCPServer.objects.filter(slug=slug).first()
        if server is None:
            notes.append(Degrade(
                'mcp', slug or '未知来源', '工具来源已不存在',
                '在智能体装配页删除该挂载或重新选择来源'))
            continue
        if not server.is_active or server.status == 'disabled':
            notes.append(Degrade(
                'mcp', server.name, '工具来源已停用，本次回答未注入',
                '在工具来源页启用后重试'))
            continue
        if server.status == 'egress_blocked':
            notes.append(Degrade(
                'mcp', server.name, '工具来源被出网开关阻断，本次回答未注入',
                '在系统设置中开放出网后点重试'))
            continue
        if server.requires_egress and not egress_allowed():
            notes.append(Degrade(
                'mcp', server.name, '全平台出网总开关为关，该云端来源本次回答未注入',
                '在系统设置中开放出网后点重试'))
            continue

        if server.transport == 'stdio':
            notes.append(Degrade(
                'mcp', server.name,
                '命令型来源在 P0 仅支持注册与连通性测试，会话调用未开放',
                '改用接口型（HTTP）来源，或等待平台版本交付常驻托管后重试'))
            continue

        present = {t.name: t for t in server.tools.filter(state='present')}
        usable = [n for n in chosen if n in present and present[n].enabled]
        vanished = [n for n in chosen if n not in present]
        switched_off = [n for n in chosen if n in present and not present[n].enabled]
        if vanished:
            notes.append(Degrade(
                'mcp', f'{server.name}·{"、".join(vanished)}',
                '工具在来源侧已不存在（下线或名称变更）',
                '在工具来源页做一次连通性测试刷新清单后重新发布'))
        if switched_off:
            notes.append(Degrade(
                'mcp', f'{server.name}·{"、".join(switched_off)}',
                '工具在来源侧已关闭',
                '在工具来源页启用该工具后重试'))
        if not usable:
            notes.append(Degrade(
                'mcp', server.name,
                '本次回答未注入该来源的任何工具' if chosen
                else '该来源未勾选任何工具',
                '在智能体装配页重新勾选工具后发布'))
            continue

        secrets = server.get_secrets()
        plans.append(MCPPlan(
            slug=server.slug,
            name=server.name,
            transport=server.transport,
            stateful=server.transport == 'stdio' or server.is_stateful,
            url=server.url,
            headers={**(server.headers or {}), **(secrets.get('headers') or {})},
            command=server.command,
            args=list(server.args or []),
            env=dict(secrets.get('env') or {}),
            cwd=str(server.cwd or ''),
            timeout=server.effective_timeout,
            tool_names=usable,
            full_names=[tool_full_name(server.slug, n) for n in usable],
            asl_names=[asl_tool_name(server.slug, n) for n in usable],
            requires_egress=bool(server.requires_egress),
        ))
        if allow:
            unselected = [n for n in plans[-1].full_names if n not in allow]
            if unselected:
                logger.debug('来源 %s 有 %d 个工具未进白名单', server.name, len(unselected))

    return plans, notes


def plan_skills(cfg: dict, user=None) -> Tuple[List[str], List[Degrade]]:
    """D3：只注入"生效正文已镜像到运行目录"的技能（C4 由镜像层保证）

    C5：技能自身的生效范围（部门灰度）在这里生效；user 为空时不做范围过滤，
    避免后台任务/非交互调用把 scope=all 的技能一并丢掉。
    """
    from backend.apps.skills.models import Skill
    from backend.apps.skills.services import sync_mirror

    dirs: List[str] = []
    notes: List[Degrade] = []
    for slug in cfg.get('skills') or []:
        skill = Skill.objects.filter(slug=slug).first()
        if skill is None:
            notes.append(Degrade('skill', slug, '技能已不存在', '在智能体装配页移除该技能'))
            continue
        if not skill.mountable:
            notes.append(Degrade(
                'skill', skill.name,
                f'技能当前为「{skill.get_status_display()}」，未生效，本次回答未注入',
                '在技能页启用该技能后重试'))
            continue
        if user is not None and not skill.applies_to(user):
            notes.append(Degrade(
                'skill', skill.name, f'{skill.scope_denial_reason(user)}，本次回答未注入',
                '联系管理员把所在部门加入生效范围，或加入定向用户名单'))
            continue
        target = skill.mirror_dir
        if not (target / (skill.entry_file or 'SKILL.md')).exists():
            sync_mirror(skill)
        if not (target / (skill.entry_file or 'SKILL.md')).exists():
            notes.append(Degrade(
                'skill', skill.name, '技能文件不在运行目录中',
                '在技能页重新保存一次以重建运行目录'))
            continue
        dirs.append(str(target))
    return dirs, notes


def plan_knowledge(cfg: dict, user) -> Tuple[List[int], List[Degrade]]:
    """E2/A4：检索范围以使用者本人的可读集合为上限，挂载不扩权"""
    from backend.apps.knowledge.models import KnowledgeBase, visible_kb_query

    raw = [i for i in (cfg.get('knowledge_base_ids') or []) if str(i).isdigit()]
    if not raw:
        return [], []

    qs = KnowledgeBase.objects.filter(pk__in=[int(i) for i in raw])
    if user is not None and not (user.is_staff or user.is_superuser):
        qs = qs.filter(visible_kb_query(user))
    allowed = {kb.pk for kb in qs}
    names = {kb.pk: kb.name for kb in KnowledgeBase.objects.filter(pk__in=[int(i) for i in raw])}

    notes = [
        Degrade('knowledge', names.get(int(i), f'#{i}'), '你没有该知识库的阅读权限',
                '联系库管理员把你加入可见范围')
        for i in raw if int(i) not in allowed
    ]
    return [int(i) for i in raw if int(i) in allowed], notes


def plan_permission(
    cfg: dict,
    plans: List[MCPPlan],
    *,
    session_grants: Optional[List[str]] = None,
    interactive: bool = True,
) -> PermissionPlan:
    """A5/D7：档位 → AgentScope 权限上下文（空 rule_content 即全量匹配该工具）

    ask 档在交互式流中为白名单外工具显式挂 ASK 规则（只读工具也会询问）；
    非交互调用回退为白名单语义，避免没有授权入口时静默挂起。
    """
    from agentscope.permission import PermissionBehavior, PermissionMode, PermissionRule

    def rule(name: str, behavior: Any) -> Dict[str, list]:
        return {name: [PermissionRule(
            tool_name=name, rule_content='', behavior=behavior, source='agenthub')]}

    asl_by_platform: Dict[str, str] = {}
    for plan in plans:
        for platform_name, asl_name in zip(plan.full_names, plan.asl_names):
            asl_by_platform[platform_name] = asl_name

    mounted = [n for plan in plans for n in plan.asl_names]
    tier = cfg.get('permission_tier')
    allow_names = {
        asl_by_platform.get(n) for n in (cfg.get('allow_tool_names') or [])
    } & set(mounted)

    if tier == 'auto':
        return PermissionPlan(mode=PermissionMode.BYPASS)

    if tier == 'ask' and interactive:
        # 会话级授权只对当前已挂载工具生效，不能凭授权复活未挂载/已失效工具
        granted = set(session_grants or []) & set(mounted)
        allow_rules: Dict[str, list] = {}
        ask_rules: Dict[str, list] = {}
        for name in mounted:
            if name in allow_names or name in granted:
                allow_rules.update(rule(name, PermissionBehavior.ALLOW))
            else:
                ask_rules.update(rule(name, PermissionBehavior.ASK))
        return PermissionPlan(
            mode=PermissionMode.DEFAULT, allow_rules=allow_rules, ask_rules=ask_rules)

    allow_rules = {}
    deny_rules: Dict[str, list] = {}
    for name in mounted:
        if name in allow_names:
            allow_rules.update(rule(name, PermissionBehavior.ALLOW))
        else:
            deny_rules.update(rule(name, PermissionBehavior.DENY))
    return PermissionPlan(
        mode=PermissionMode.DEFAULT, allow_rules=allow_rules, deny_rules=deny_rules)


# ─── 装配入口 ───

def build_clients(plans: List[MCPPlan]) -> Tuple[List[Any], List[Any]]:
    """按来源建 AgentScope 客户端（stdio 必须先 connect，见模块文档）"""
    from agentscope.mcp import MCPClient, StdioMCPConfig

    clients: List[Any] = []
    stateful: List[Any] = []
    for plan in plans:
        if plan.stateful:
            client = MCPClient(
                name=plan.client_name,
                is_stateful=True,
                mcp_config=StdioMCPConfig(
                    command=plan.command,
                    args=plan.args,
                    env={**os.environ, **plan.env} if plan.env else None,
                    cwd=plan.cwd or None,
                ),
                enable_tools=plan.tool_names,
                execution_timeout=float(plan.timeout),
            )
            stateful.append(client)
        else:
            clients.append(build_http_client(plan))
    return clients, stateful


def build_http_client(plan: MCPPlan) -> Any:
    """HTTP 工具来源 → AgentScope stateless 客户端（每次调用临时建会话）"""
    from agentscope.mcp import HttpMCPConfig, MCPClient

    return MCPClient(
        name=plan.client_name,
        is_stateful=False,
        mcp_config=HttpMCPConfig(
            url=plan.url,
            headers=plan.headers or None,
            timeout=float(plan.timeout),
        ),
        enable_tools=plan.tool_names,
        execution_timeout=float(plan.timeout),
    )


async def assemble(
    *,
    cfg: dict,
    user=None,
    user_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    stream: bool = True,
    temperature: Optional[float] = None,
    session_grants: Optional[List[str]] = None,
    interactive: bool = True,
    on_knowledge_sources: Optional[Callable[[List[dict]], None]] = None,
    preference_prompt: Optional[str] = None,
) -> Assembled:
    """按快照装配一个可执行会话（协程，与 reply_stream 同事件循环）

    同步 ORM 一律切线程池，技能目录以镜像目录字符串注入 Toolkit。
    """
    from agentscope.agent import Agent, ReActConfig
    from agentscope.permission import PermissionContext
    from agentscope.skill import LocalSkillLoader
    from agentscope.state import AgentState
    from agentscope.tool import Toolkit

    from backend.apps.agents.core.factory import create_chat_model
    from backend.apps.agents.tools import (
        build_chat_tools, build_multi_knowledge_tools, build_skill_file_tools,
    )

    thread = _sync
    plans, notes = await thread(plan_mcp)(cfg)
    skill_dirs, skill_notes = await thread(plan_skills)(cfg, user)
    notes += skill_notes
    kb_ids, kb_notes = await thread(plan_knowledge)(cfg, user)
    notes += kb_notes
    permission = plan_permission(
        cfg, plans, session_grants=session_grants, interactive=interactive)

    if cfg.get('permission_tier') == 'ask' and not interactive:
        notes.append(Degrade(
            'permission', '逐次询问',
            '当前调用为非流式，没有会话内授权入口，本次按白名单档运行（未勾选的工具被拒绝）',
            '改用流式会话，或把需要使用的工具加入白名单'))

    builtin = list(cfg.get('builtin_capabilities') or [])
    tools: List[Any] = []
    if 'generate_file' in builtin and user_id is not None:
        tools += build_chat_tools(user_id, None, conversation_id)
    if 'knowledge_search' in builtin:
        if kb_ids:
            tools += build_multi_knowledge_tools(
                kb_ids, user_id, source_sink=on_knowledge_sources)
        else:
            notes.append(Degrade(
                'knowledge', '资料检索', '该智能体未挂载可用知识库',
                '在智能体装配页挂载知识库后发布'))

    mcps, stateful = build_clients(plans)
    for client in stateful:
        try:
            await client.connect()
            mcps.append(client)
        except Exception as exc:
            notes.append(Degrade(
                'mcp', client.name, f'工具来源连接失败：{exc}',
                '在工具来源页检查启动命令后点重试'))

    skill_loaders = [LocalSkillLoader(directory=d) for d in skill_dirs]

    # C1：只有本次真正注入的技能包才暴露附件只读工具
    skill_roots = {Path(d).name: d for d in skill_dirs}
    tools += build_skill_file_tools(skill_roots)

    toolkit = Toolkit(
        tools=tools or None,
        skills_or_loaders=skill_loaders or None,
        mcps=mcps or None,
    )

    prompt = render_prompt(cfg, user)
    if kb_ids and 'knowledge_search' in builtin:
        prompt += (KB_PROMPT_ALWAYS if cfg.get('knowledge_mode') == 'always'
                   else KB_PROMPT_SUFFIX)
        if cfg.get('followup_rewrite', True):
            prompt += KB_PROMPT_FOLLOWUP
    if 'generate_file' in builtin:
        prompt += FILE_PROMPT_SUFFIX
    if skill_dirs:
        prompt += SKILL_PACKAGE_PROMPT_SUFFIX
    # D8：用户确认的跨会话偏好固定在系统提示词末尾，作为用户输入对待
    if preference_prompt:
        prompt += '\n\n' + preference_prompt

    state = AgentState(permission_context=PermissionContext(
        mode=permission.mode,
        allow_rules=permission.allow_rules,
        ask_rules=permission.ask_rules,
        deny_rules=permission.deny_rules,
    ))

    model = await thread(create_chat_model)(
        stream=stream,
        model_name=cfg.get('model') or None,
        temperature=temperature if temperature is not None else cfg.get('temperature'),
        top_p=cfg.get('top_p'),
        enable_thinking=bool(cfg.get('enable_thinking')),
    )

    agent = Agent(
        name=cfg.get('name') or '企业智能体',
        system_prompt=prompt,
        model=model,
        toolkit=toolkit,
        state=state,
        # A3「查询次数上限」= 一轮对话内允许的工具往返次数；
        # AgentScope 的 max_iters 含最后一次作答轮，故留 2 次余量
        react_config=ReActConfig(
            max_iters=int(cfg.get('max_tool_rounds') or 3) + 2),
    )

    displays: Dict[str, str] = {}
    egress_tools: set = set()
    egress_hosts: Dict[str, str] = {}
    for plan in plans:
        host = (urlparse(plan.url or '').hostname or '').strip().lower()
        for asl_name in plan.asl_names:
            displays[asl_name] = plan.display_of(asl_name)
            if plan.requires_egress:
                egress_tools.add(asl_name)
                egress_hosts[asl_name] = host

    return Assembled(agent=agent, notes=notes, clients=stateful,
                     displays=displays, egress_tools=egress_tools,
                     egress_hosts=egress_hosts)


async def create_profile_runtime(
    agent_id: int,
    *,
    user=None,
    user_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    stream: bool = True,
    temperature: Optional[float] = None,
    draft: bool = False,
    agent_version: Optional[int] = None,
    session_grants: Optional[List[str]] = None,
    interactive: bool = True,
    on_knowledge_sources: Optional[Callable[[List[dict]], None]] = None,
    preference_prompt: Optional[str] = None,
) -> Tuple[Optional[Assembled], List[Degrade]]:
    """按 agent_id 装配；智能体不可用（不存在/无权限）时返回 (None, 说明)

    draft=True 为 A3 试聊路径：固定按最新保存的草稿配置装配，
    且不再给「该智能体尚未发布」的降级说明（试聊测的本来就是未发布改动）。
    agent_version 用于把会话钉在开场版本（7.3）。
    """
    cfg, agent, notes = await _sync(resolve_snapshot)(
        agent_id, user, draft=draft, agent_version=agent_version)
    if cfg is None:
        return None, notes

    if not draft and not (agent.is_builtin or agent.status == 'published'):
        notes.append(Degrade(
            'agent', cfg.get('name') or '', '该智能体尚未发布，仅创建者可在调试中使用'))

    bundle = await assemble(
        cfg=cfg, user=user, user_id=user_id,
        conversation_id=conversation_id, stream=stream, temperature=temperature,
        session_grants=session_grants, interactive=interactive,
        on_knowledge_sources=on_knowledge_sources,
        preference_prompt=preference_prompt,
    )
    bundle.notes = notes + bundle.notes
    return bundle, bundle.notes
