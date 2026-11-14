"""智能体装配服务层（PRD 第 6 章 A 组：A1–A7 的口径都在这里）

职责边界：模型层承载状态与快照，本层承载三件跨模型的事——
1. 可见范围与权限（A1 草稿仅创建者与管理员可见、A6 部门含子部门）
2. 能力挂载校验（A4 逐个勾选工具、未启用技能不可选、来源异常只标黄不阻断）
3. 发布前检查与差异视图（A7 三档结论逐项清单 + 与线上版本的分区差异）

全平台工具唯一标识口径（7.1）：唯一键用 mcp__{来源slug}__{工具名}，
界面展示用「来源名·工具名」；白名单、启停、审计、过程区同口径，本层是唯一换算处。
"""
import json
import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from backend.apps.agenthub.models import (
    BUILTIN_CAPABILITIES, RETRIEVAL_PRESETS, SPEECH_PRESETS,
    AgentMCPServer, AgentProfile, AgentSkill, AgentStatus,
    AgentVersion, KnowledgeMode, PermissionTier, RetrievalTier, SpeechTier,
)
from backend.apps.common.choices import Visibility
from backend.apps.settings.models import SystemSetting

logger = logging.getLogger(__name__)

SPEECH_LABELS = dict(SpeechTier.choices)
RETRIEVAL_LABELS = dict(RetrievalTier.choices)
VISIBILITY_LABELS = dict(Visibility.choices)
STATUS_LABELS = dict(AgentStatus.choices)

# A3 提示词占位符（界面可插入，运行时替换）
PROMPT_PLACEHOLDERS = [
    {'token': '{current_date}', 'desc': '当天日期'},
    {'token': '{user_name}', 'desc': '使用者姓名'},
    {'token': '{department}', 'desc': '使用者所属部门'},
]

# A5 档位 → AgentScope PermissionMode；D7 闭环后「逐次询问」正式可选
PERMISSION_TIER_INFO = [
    {'value': PermissionTier.WHITELIST, 'label': '白名单', 'selectable': True,
     'mode': 'DEFAULT + allow_rules', 'desc': '只允许勾选过的工具被调用，其余一律不放行'},
    {'value': PermissionTier.AUTO, 'label': '自动放行', 'selectable': True,
     'mode': 'BYPASS', 'desc': '工具调用不再逐次确认，适合只读查询类智能体'},
    {'value': PermissionTier.ASK, 'label': '逐次询问', 'selectable': True,
     'mode': 'DEFAULT + ask_rules',
     'desc': '白名单内工具直接放行，其余每次调用弹出授权卡片；60 秒未响应按拒绝处理'},
]

SECTION_TITLES = {
    'basic': '基本信息',
    'model': '模型与说话方式',
    'mcp_servers': '工具来源',
    'skills': '技能',
    'knowledge_bases': '知识库',
    'preferences': '跨会话偏好',
    'builtin_capabilities': '平台内置能力',
    'permission': '授权档位',
    'visibility': '可见范围',
    'created': '本次新建',
}

FIELD_LABELS = {
    'name': '名称', 'description': '用途描述', 'job_tags': '岗位标签', 'icon': '图标',
    'model_name': '模型', 'speech_tier': '说话方式', 'retrieval_tier': '查询次数上限',
    'knowledge_mode': '检索调用策略', 'followup_rewrite': '追问补全',
    'default_preferences': '默认偏好',
    'enable_thinking': '思考过程', 'prompt': '提示词',
    'mcp_servers': '工具来源', 'skills': '技能', 'knowledge_base_ids': '知识库',
    'builtin_capabilities': '内置能力',
    'permission_tier': '授权档位', 'allow_tool_names': '白名单工具',
    'visibility': '可见范围', 'allowed_departments': '可见部门',
    'include_sub_departments': '含子部门',
}

# 未发布的修改是否算"有实质变化"——A7 差异视图与 A8 变更摘要共用
PROMPT_SUMMARY_LEN = 40

SOURCE_UNVERIFIED_STATUSES = {'unknown', 'error'}


def tool_full_name(server_slug: str, tool_name: str) -> str:
    """唯一键口径（7.1）：授权规则、白名单、审计、过程区一律用它"""
    return f'mcp__{server_slug}__{tool_name}'


def tool_display_name(server_name: str, tool_name: str) -> str:
    """界面口径（7.1）：来源名·工具名"""
    return f'{server_name}·{tool_name}'


# ─── A1/A6 可见性与权限 ───

def visible_agent_query(user):
    """列表可见集合：管理员全量；其他人 = 自己的草稿 + 已发布且命中可见范围

    A1 明确"草稿仅创建者与管理员可见"，因此非创建者的草稿一律不出现在列表与详情中；
    已停用/已归档不在用户侧选择器（A6 停用后用户侧移除），但历史会话仍可读，
    故已发布→已停用的智能体对原本可见范围成员仍可见于"我的会话"，管理端不受此限制。
    A6「仅管理端」不进入任何业务用户可见范围（含其创建者），只有管理员可见。
    """
    if user.is_staff or user.is_superuser:
        return Q()
    own = user_department_id(user)
    if own is None:
        department_q = Q(pk__in=[])  # 没有部门的人只可能看到公开范围
    else:
        department_q = (
            Q(allowed_departments=own)
            | Q(include_sub_departments=True,
                allowed_departments__in=related_department_ids(user))
        )
    return ~Q(visibility=Visibility.ADMIN_ONLY) & (
        Q(created_by=user)
        | (Q(status=AgentStatus.PUBLISHED)
           & (Q(visibility=Visibility.PUBLIC)
              | (Q(visibility=Visibility.DEPARTMENT) & department_q)))
    )


def user_department_id(user):
    """用户所在部门 ID（不含上级），A6 不含子部门时按此精确比对"""
    from backend.apps.users.models import Department

    if not getattr(user, 'department', ''):
        return None
    return Department.objects.filter(name=user.department).values_list(
        'pk', flat=True).first()


def related_department_ids(user):
    """用户所在部门 + 全部上级部门（上级被授予的可见性对下级同样生效）"""
    from backend.apps.users.models import Department

    own = user_department_id(user)
    if own is None:
        return []
    ids = set()
    dept = Department.objects.filter(pk=own).first()
    while dept is not None:
        ids.add(dept.pk)
        dept = Department.objects.filter(pk=dept.parent_id).first() if dept.parent_id else None
    return ids


def scoped_department_ids(agent) -> set:
    """A6：可见部门集合，勾选"含子部门"时展开为所选部门的全部后代"""
    chosen = list(agent.allowed_departments.values_list('pk', flat=True))
    if not chosen:
        return set(chosen)
    if not agent.include_sub_departments:
        return set(chosen)
    from backend.apps.users.models import Department

    ids = set()
    for pk in chosen:
        dept = Department.objects.filter(pk=pk).first()
        if dept is not None:
            ids |= dept.descendant_ids()
    return ids


def can_see(user, agent) -> bool:
    if user.is_staff or user.is_superuser:
        return True
    # A6「仅管理端」：业务用户彻底不可见（创建者本人也不例外），先于创建者分支判定
    if agent.visibility == Visibility.ADMIN_ONLY:
        return False
    if agent.created_by_id == user.pk:
        return True
    if agent.status != AgentStatus.PUBLISHED:
        return False
    if agent.visibility == Visibility.PUBLIC:
        return True
    if agent.visibility == Visibility.DEPARTMENT:
        own = user_department_id(user)
        return own is not None and own in scoped_department_ids(agent)
    return False


def can_edit(user, agent) -> bool:
    """装配面权限：管理员可改任何智能体；普通用户只能改自己创建的非内置智能体"""
    if user.is_staff or user.is_superuser:
        return True
    return agent.created_by_id == user.pk and not agent.is_builtin


def occupant_of_name(name: str, exclude_pk=None) -> AgentProfile | None:
    """A2 名称冲突时提示占用方（谁在用这个名字、什么状态）"""
    qs = AgentProfile.objects.filter(name=name)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.first()


# ─── 模型可用性（A3：从"已验证可用"列表中选） ───

def verified_models() -> list:
    """系统设置中已验证可用的模型名列表

    平台在系统设置里跑过连通性测试的模型才进这个列表；没配置时退回数据库里的
    当前主模型，保证装配页选择器永不为空白（A3 要求显式提示，不允许静默）。
    环境变量不参与：数据库没有主模型时返回空列表，由调用方提示去平台配置。
    """
    raw = SystemSetting.get_value('llm_verified_models', '')
    names = []
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                names = [str(v).strip() for v in parsed if str(v).strip()]
        except (TypeError, ValueError):
            logger.warning('llm_verified_models 不是合法 JSON，已忽略：%s', raw[:80])
    if names:
        return names
    current = SystemSetting.get_value('llm_model', '') or ''
    return [current] if current else []


def model_available(model_name: str) -> bool:
    options = verified_models()
    if not options:
        return False
    if model_name in options:
        return True
    # 兼容带厂商前缀/大小写差异的写法，但仍以列表为准
    lowered = {m.lower(): m for m in options}
    return model_name.lower() in lowered


# ─── A4 能力挂载 ───

def mounted_full_names(agent) -> set:
    """当前挂载的全部工具唯一键（白名单档位校验与差异视图共用）"""
    out = set()
    for row in agent.agentmcpserver_set.select_related('server'):
        slug = row.server.slug
        for name in (row.tool_names or []):
            out.add(tool_full_name(slug, name))
    return out


def tool_conflicts(agent) -> list:
    """A4：不同来源同名工具 → 保留「来源名·工具名」全称并标注冲突"""
    by_tool = {}
    for row in agent.agentmcpserver_set.select_related('server'):
        for name in (row.tool_names or []):
            by_tool.setdefault(name, []).append(
                tool_display_name(row.server.name, name))
    return [{'tool': name, 'owners': owners}
            for name, owners in sorted(by_tool.items()) if len(owners) > 1]


def _check_server(server, tool_names):
    """来源级校验：停用/出网未开放不可选；未验证与异常允许挂载但带黄标"""
    if not server.is_active or server.status == 'disabled':
        raise ValidationError({'mcp_mounts': f'工具来源「{server.name}」已停用，不可挂载'})
    if server.status == 'egress_blocked':
        raise ValidationError({
            'mcp_mounts': f'工具来源「{server.name}」需要出网而平台出网总开关未开放，'
                          f'请先在工具来源页开放出网后再挂载'})
    if not tool_names:
        return []
    present = {t.name: t for t in server.tools.filter(state='present')}
    unknown = [n for n in tool_names if n not in present]
    if unknown:
        raise ValidationError({
            'mcp_mounts': f'来源「{server.name}」内不存在这些工具：{"、".join(unknown)}；'
                          f'请先在来源页做一次连通性测试刷新工具清单'})
    disabled = [n for n in tool_names if not present[n].enabled]
    if disabled:
        raise ValidationError({
            'mcp_mounts': f'这些工具在来源侧已关闭，不可勾选：{"、".join(disabled)}；'
                          f'如需使用请先在来源页启用'})
    return tool_names


def _check_skill(skill):
    """A4：未启用技能灰置不可选并给原因"""
    from backend.apps.skills.models import SkillStatus

    if skill.mountable:
        return
    reason = {
        SkillStatus.INVALID: '技能校验未通过，请先在技能页修复',
        SkillStatus.PENDING: '技能待审核且无生效版本',
        SkillStatus.DISABLED: '技能已停用',
    }.get(skill.status, f'技能当前状态为「{skill.get_status_display()}」，不可挂载')
    raise ValidationError({'skill_ids': f'技能「{skill.name}」不可挂载：{reason}'})


def sync_mounts(agent, *, mcp_mounts=None, skill_ids=None, knowledge_base_ids=None,
                builtin_capabilities=None, actor=None) -> dict:
    """A4：整体覆盖式挂载（四个分区均可为空）

    mcp_mounts 形如 [{"server": id, "tool_names": ["a","b"]}]，
    工具名必须是该来源内已发现且来源侧已启用的工具，否则显式报错（禁止静默丢弃）。
    """
    changed = {'mcp_servers': 0, 'skills': 0, 'knowledge_bases': 0, 'builtin': 0}

    from backend.apps.mcp.models import MCPServer

    if mcp_mounts is not None:
        if not isinstance(mcp_mounts, list):
            raise ValidationError({'mcp_mounts': '工具来源需为列表'})
        rows, seen = [], set()
        for item in mcp_mounts:
            if not isinstance(item, dict) or not item.get('server'):
                raise ValidationError({'mcp_mounts': '每项需形如 {"server": ID, "tool_names": []}'})
            server_id = item['server']
            server = MCPServer.objects.filter(pk=server_id).first()
            if server is None:
                raise ValidationError({'mcp_mounts': f'工具来源 {server_id} 不存在'})
            names = item.get('tool_names') or []
            if not isinstance(names, list):
                raise ValidationError({'mcp_mounts': f'来源「{server.name}」的工具需为名称数组'})
            names = [str(n) for n in names]
            if server.pk in seen:
                raise ValidationError({'mcp_mounts': f'来源「{server.name}」重复挂载'})
            seen.add(server.pk)
            _check_server(server, names)
            rows.append((server, sorted(set(names))))
        AgentMCPServer.objects.filter(agent=agent).delete()
        for server, names in rows:
            AgentMCPServer.objects.create(agent=agent, server=server, tool_names=names)
        changed['mcp_servers'] = len(rows)

    if skill_ids is not None:
        from backend.apps.skills.models import Skill

        ids = [int(i) for i in skill_ids or []]
        skills = list(Skill.objects.filter(pk__in=ids))
        missing = set(ids) - {s.pk for s in skills}
        if missing:
            raise ValidationError({'skill_ids': f'技能不存在：{sorted(missing)}'})
        for skill in skills:
            _check_skill(skill)
        AgentSkill.objects.filter(agent=agent).delete()
        for skill in skills:
            AgentSkill.objects.create(agent=agent, skill=skill)
        changed['skills'] = len(skills)

    if knowledge_base_ids is not None:
        from backend.apps.knowledge.models import KnowledgeBase

        ids = [int(i) for i in knowledge_base_ids or []]
        found = set(KnowledgeBase.objects.filter(pk__in=ids).values_list('pk', flat=True))
        missing = sorted(set(ids) - found)
        if missing:
            raise ValidationError({'knowledge_base_ids': f'知识库不存在：{missing}'})
        agent.knowledge_base_ids = ids
        agent.save(update_fields=['knowledge_base_ids', 'updated_at'])
        changed['knowledge_bases'] = len(ids)

    if builtin_capabilities is not None:
        allowed = {key for key, _ in BUILTIN_CAPABILITIES}
        values = list(builtin_capabilities or [])
        unknown = [v for v in values if v not in allowed]
        if unknown:
            raise ValidationError({'builtin_capabilities':
                                   f'不支持的内置能力：{"、".join(map(str, unknown))}'})
        agent.builtin_capabilities = values
        agent.save(update_fields=['builtin_capabilities', 'updated_at'])
        changed['builtin'] = len(values)

    if actor is not None:
        agent.updated_by = actor
        agent.save(update_fields=['updated_by', 'updated_at'])
    return changed


def sync_allow_list(agent, allow_tool_names=None) -> None:
    """A5：白名单档位落库的唯一键列表

    未显式给出时同步为"当前挂载的全部工具"；显式给出时逐项校验，
    不在挂载集合内的必须报错而不是静默忽略（否则现场会以为已放行）。
    """
    mounted = mounted_full_names(agent)
    if allow_tool_names is None:
        agent.allow_tool_names = sorted(mounted)
    else:
        values = sorted(set(str(v) for v in allow_tool_names))
        unknown = [v for v in values if v not in mounted]
        if unknown:
            raise ValidationError({
                'allow_tool_names': f'这些工具未挂载在该智能体上，无法加入白名单：'
                                    f'{"、".join(unknown[:8])}'})
        agent.allow_tool_names = values
    agent.save(update_fields=['allow_tool_names', 'updated_at'])


# ─── A7 发布前检查（三档结论，逐项列出） ───

def _item(level, code, section, message, field=None):
    return {'level': level, 'code': code, 'section': section,
            'section_title': SECTION_TITLES.get(section, section),
            'label': FIELD_LABELS.get(field or '', code), 'field': field,
            'message': message}


def prepublish_check(agent) -> dict:
    """A7 发布前检查清单：逐项给结论，不一次性抛错

    阻断（红）＝不补齐就发不出去；警示（黄）＝勾选"我已了解风险"后可放行，
    勾选行为入审计；提示（灰）＝只告知不拦。
    """
    blocking, warnings, notices = [], [], []

    if not (agent.name or '').strip() or len(agent.name.strip()) < 2:
        blocking.append(_item('blocking', 'name_missing', 'basic',
                              '名称缺失或不足 2 个字', 'name'))
    if not (agent.description or '').strip():
        blocking.append(_item('blocking', 'description_missing', 'basic',
                              '用途描述未填写，用户无法判断该找它做什么', 'description'))
    if not (agent.prompt or '').strip():
        blocking.append(_item('blocking', 'prompt_missing', 'model',
                              '提示词为空，智能体没有可执行的作答规范', 'prompt'))
    if not (agent.model_name or '').strip():
        blocking.append(_item('blocking', 'model_missing', 'model',
                              '未选定模型', 'model_name'))
    elif not model_available(agent.model_name):
        options = verified_models()
        blocking.append(_item('blocking', 'model_unavailable', 'model',
                              f'模型「{agent.model_name}」不在系统设置的已验证可用列表内'
                              f'（当前可选：{"、".join(options) or "无，请先在系统设置验证模型"}）',
                              'model_name'))
    if agent.speech_tier not in SPEECH_LABELS:
        blocking.append(_item('blocking', 'speech_tier_invalid', 'model',
                              '说话方式档位非法，必须选三档预设之一', 'speech_tier'))
    if agent.retrieval_tier not in RETRIEVAL_LABELS:
        blocking.append(_item('blocking', 'retrieval_tier_invalid', 'model',
                              '查询次数上限档位非法，必须选三档预设之一', 'retrieval_tier'))
    if not agent.permission_tier:
        blocking.append(_item('blocking', 'permission_missing', 'permission',
                              '授权档位未选定，不允许留空', 'permission_tier'))
    elif agent.permission_tier == PermissionTier.ASK:
        warnings.append(_item('warning', 'permission_ask_timeout', 'permission',
                              '「逐次询问」档位会在会话内为白名单外工具弹授权卡片，'
                              '60 秒未响应按拒绝处理',
                              'permission_tier'))

    mounts = list(agent.agentmcpserver_set.select_related('server'))
    for row in mounts:
        if row.server.status in SOURCE_UNVERIFIED_STATUSES:
            state = '未验证' if row.server.status == 'unknown' else '连接异常'
            detail = row.server.last_error or ''
            warnings.append(_item('warning', f'server_{row.server.status}', 'mcp_servers',
                                  f'工具来源「{row.server.name}」{state}'
                                 f'{f"（{detail[:60]}）" if detail else ""}，'
                                  f'发布后该来源的工具在会话里可能不可用'))
        elif row.server.status == 'disabled':
            warnings.append(_item('warning', 'server_disabled', 'mcp_servers',
                                  f'工具来源「{row.server.name}」已被停用，'
                                  f'会话中该来源会降级为不可用'))
        if row.server.transport == 'stdio':
            warnings.append(_item('warning', 'server_stdio_not_injected', 'mcp_servers',
                                  f'工具来源「{row.server.name}」是命令型来源，P0 仅支持'
                                  f'注册与连通性测试，会话中不注入其工具（常驻托管随平台'
                                  f'版本交付）；业务调用请改用接口型来源'))
        if not (row.tool_names or []):
            notices.append(_item('notice', 'server_no_tools', 'mcp_servers',
                                 f'工具来源「{row.server.name}」已挂载但未勾选任何工具，'
                                 f'该来源在本次发布中不产生能力'))

    allow = set(agent.allow_tool_names or [])
    mounted = mounted_full_names(agent)
    outside = sorted(allow - mounted)
    if outside:
        blocking.append(_item('blocking', 'allow_not_mounted', 'permission',
                              f'白名单里有 {len(outside)} 个工具并未挂载：'
                              f'{"、".join(outside[:6])}，请重新勾选工具或清空白名单',
                              'allow_tool_names'))
    if agent.permission_tier == PermissionTier.WHITELIST and not allow:
        notices.append(_item('notice', 'whitelist_empty', 'permission',
                            '白名单档下未放行任何工具，智能体只能纯对话'
                           + ('（已挂载工具但未勾选白名单）' if mounted else ''),
                            'allow_tool_names'))

    skill_count = agent.skills.count()
    if skill_count and not _prompt_mentions_read_skill(agent.prompt):
        notices.append(_item('notice', 'skill_not_referenced', 'skills',
                            f'已挂载 {skill_count} 项技能，但提示词没有要求'
                            '「先读技能规范再作答」，技能可能不被使用'))

    # C5：技能自身的生效范围（部门 / 灰度比例 / 定向用户），发布前让管理员明确知道哪些人拿到
    from backend.apps.skills.models import SkillScope

    for skill in agent.skills.all():
        dept_names = list(skill.applicable_departments.values_list('name', flat=True))
        if skill.scope_mode == SkillScope.DEPARTMENTS and not dept_names:
            warnings.append(_item(
                'warning', 'skill_scope_empty', 'skills',
                f'技能「{skill.name}」设为按部门生效但未选择任何部门，'
                f'运行时只会对定向用户注入', 'skill_ids'))
        elif skill.scope_mode == SkillScope.DEPARTMENTS:
            suffix = '（含子部门）' if skill.scope_include_sub_departments else ''
            notices.append(_item(
                'notice', 'skill_scope_limited', 'skills',
                f'技能「{skill.name}」仅对 {"、".join(dept_names[:6])}{suffix} 生效，'
                f'其他部门用户不会获得该规范'))
        if skill.rollout_percent < 100:
            notices.append(_item(
                'notice', 'skill_gray_percent', 'skills',
                f'技能「{skill.name}」在基础范围内仅对 {skill.rollout_percent}% 的用户生效'
                f'（按用户稳定分桶，比例上调只增不减）'))
        targeted = skill.applicable_users.count()
        if targeted:
            notices.append(_item(
                'notice', 'skill_scope_users', 'skills',
                f'技能「{skill.name}」额外对 {targeted} 名定向用户生效，'
                f'不受部门与灰度比例限制'))

    kb_ids = [int(i) for i in (agent.knowledge_base_ids or []) if str(i).isdigit()]
    if kb_ids:
        from backend.apps.knowledge.models import KnowledgeBase

        rows = {kb.pk: kb for kb in KnowledgeBase.objects.filter(pk__in=kb_ids)}
        empty = [kb.name for kb in rows.values() if not kb.document_count]
        gone = [i for i in kb_ids if i not in rows]
        if gone:
            blocking.append(_item('blocking', 'kb_missing', 'knowledge_bases',
                                  f'挂载的知识库已不存在：{gone}，请重新选择',
                                  'knowledge_base_ids'))
        if empty and len(empty) == len(rows):
            notices.append(_item('notice', 'kb_all_empty', 'knowledge_bases',
                                 '挂载的知识库内均无有效资料，资料检索不会返回内容'))
        elif empty:
            notices.append(_item('notice', 'kb_partial_empty', 'knowledge_bases',
                                 f'这些知识库内暂无有效资料：{"、".join(empty)}'))

    missing_servers = _skill_requires_missing(agent)
    for skill_name, full_names in missing_servers:
        notices.append(_item('notice', 'skill_requires_missing', 'skills',
                             f'技能「{skill_name}」声明依赖工具 '
                             f'{"、".join(full_names[:4])}，当前未挂载对应来源'))

    previous = AgentVersion.objects.filter(agent=agent).order_by('-version').first()
    diff = build_diff(agent, previous)
    return {
        'agent_id': agent.pk,
        'status': ('blocked' if blocking
                   else ('ack_required' if warnings else 'pass')),
        'can_publish': not blocking,
        'requires_ack': bool(warnings),
        'first_publish': previous is None,
        'base_version': previous.version if previous else 0,
        'next_version': agent.current_version + 1,
        'blocking': blocking,
        'warnings': warnings,
        'notices': notices,
        'counts': {'blocking': len(blocking), 'warnings': len(warnings),
                   'notices': len(notices)},
        'diff': diff,
    }


_SKILL_READ_HINTS = ('先读', '先查看', '阅读', '按照技能', '技能规范', 'SKILL', '规范作答')


def _prompt_mentions_read_skill(prompt: str) -> bool:
    text = (prompt or '').lower()
    return any(hint.lower() in text for hint in _SKILL_READ_HINTS)


def _skill_requires_missing(agent) -> list:
    """技能声明依赖的工具（Skill.requires 唯一键）与已挂载来源比对"""
    mounted_full = mounted_full_names(agent)
    out = []
    for link in AgentSkill.objects.filter(agent=agent).select_related('skill'):
        requires = list(link.skill.requires or [])
        missing = [r for r in requires if r not in mounted_full]
        if missing:
            out.append((link.skill.name, missing))
    return out


# ─── A7 差异视图 / A8 变更摘要 ───

def _prompt_summary(text: str) -> str:
    """A7：差异视图里提示词只给前若干字摘要 + 字数增减，不摊全文"""
    body = (text or '').replace('\n', ' ')
    if len(body) <= PROMPT_SUMMARY_LEN:
        return body
    return body[:PROMPT_SUMMARY_LEN] + '…'


def _choice_label(field, value):
    if field == 'speech_tier':
        return SPEECH_LABELS.get(value, value)
    if field == 'retrieval_tier':
        return RETRIEVAL_LABELS.get(value, value)
    if field == 'permission_tier':
        return dict(PermissionTier.choices).get(value, value)
    if field == 'knowledge_mode':
        return dict(KnowledgeMode.choices).get(value, value)
    if field == 'visibility':
        return dict(Visibility.choices).get(value, value)
    return value


def _server_map(snapshot: dict) -> dict:
    return {m['server']: sorted(m.get('tool_names') or [])
            for m in (snapshot.get('mcp_servers') or [])}


def _server_display(snapshot: dict, slug: str) -> str:
    for m in snapshot.get('mcp_servers') or []:
        if m['server'] == slug:
            return m.get('server_name') or slug
    return slug


def _diff_mcp_servers(old_snap: dict, new_snap: dict) -> list:
    old, new = _server_map(old_snap), _server_map(new_snap)
    items = []
    for slug in sorted(set(new) - set(old)):
        tools = new[slug]
        items.append({'kind': 'added', 'server': _server_display(new_snap, slug),
                      'tools': tools, 'detail': f'新增来源，勾选 {len(tools)} 个工具'})
    for slug in sorted(set(old) - set(new)):
        items.append({'kind': 'removed', 'server': _server_display(old_snap, slug),
                      'tools': old[slug],
                      'detail': f'移除来源（原勾选 {len(old[slug])} 个工具）'})
    for slug in sorted(set(old) & set(new)):
        added = sorted(set(new[slug]) - set(old[slug]))
        removed = sorted(set(old[slug]) - set(new[slug]))
        if added or removed:
            items.append({'kind': 'tools', 'server': _server_display(new_snap, slug),
                          'added': added, 'removed': removed,
                          'detail': f'工具新增 {len(added)} 个、移除 {len(removed)} 个'})
    return items


def _names_of(ids, model) -> dict:
    from django.apps import apps

    cls = apps.get_model(model)
    return {obj.pk: obj.name for obj in cls.objects.filter(pk__in=ids)}


def diff_snapshots(old_snap: dict, new_snap: dict) -> dict:
    """两份配置快照的逐分区差异（A7 差异视图与 A8 版本对比共用）

    提示词只给摘要与字数增减；列表按新增/删除拆分并给出数量变化；
    工具来源细到逐个工具。返回的 sections 可直接渲染。
    """
    raw = AgentVersion.diff_of(old_snap, new_snap)
    sections = []
    for key, fields in raw.items():
        items = []
        for field, change in fields.items():
            old_v, new_v = change.get('old'), change.get('new')
            if field == 'prompt':
                items.append({'field': 'prompt', 'label': '提示词', 'kind': 'text',
                              'old_summary': _prompt_summary(old_v),
                              'old_length': len(old_v or ''),
                              'new_summary': _prompt_summary(new_v),
                              'new_length': len(new_v or ''),
                              'delta': len(new_v or '') - len(old_v or '')})
            elif field == 'mcp_servers':
                for row in _diff_mcp_servers(old_snap, new_snap):
                    items.append({'field': 'mcp_servers',
                                  'label': f'工具来源：{row["server"]}', **row})
            elif field == 'skills':
                added = sorted(set(new_v or []) - set(old_v or []))
                removed = sorted(set(old_v or []) - set(new_v or []))
                items.append({'field': 'skills', 'label': '技能', 'kind': 'list',
                              'added': added, 'removed': removed,
                              'detail': f'新增 {len(added)} 项、移除 {len(removed)} 项'})
            elif field == 'knowledge_base_ids':
                names_old = _names_of(old_v or [], 'knowledge.KnowledgeBase')
                names_new = _names_of(new_v or [], 'knowledge.KnowledgeBase')
                added = sorted(set(names_new) - set(names_old))
                removed = sorted(set(names_old) - set(names_new))
                items.append({'field': 'knowledge_base_ids', 'label': '知识库',
                              'kind': 'list',
                              'added': [names_new[i] for i in added],
                              'removed': [names_old[i] for i in removed],
                              'detail': f'新增 {len(added)} 个、移除 {len(removed)} 个'})
            elif field in ('job_tags', 'builtin_capabilities', 'allowed_departments',
                           'allow_tool_names'):
                added = sorted(set(new_v or []) - set(old_v or []))
                removed = sorted(set(old_v or []) - set(new_v or []))
                items.append({'field': field, 'label': FIELD_LABELS.get(field, field),
                              'kind': 'list', 'added': added, 'removed': removed,
                              'detail': f'新增 {len(added)}、移除 {len(removed)}'})
            elif isinstance(new_v, bool) or isinstance(old_v, bool):
                items.append({'field': field, 'label': FIELD_LABELS.get(field, field),
                              'kind': 'bool',
                              'old': '开' if old_v else '关', 'new': '开' if new_v else '关'})
            else:
                items.append({'field': field, 'label': FIELD_LABELS.get(field, field),
                              'kind': 'value',
                              'old': _choice_label(field, old_v),
                              'new': _choice_label(field, new_v),
                              'old_display': _choice_label(field, old_v),
                              'new_display': _choice_label(field, new_v)})
        sections.append({'key': key, 'title': SECTION_TITLES.get(key, key), 'items': items})
    return {'no_change': not sections, 'sections': sections,
            'summary': ('无实质变化' if not sections
                        else '、'.join(s['title'] for s in sections))}


def build_diff(agent, previous: AgentVersion | None = None) -> dict:
    """A7 差异视图：按分区列出与上一个已发布版本的实质变化"""
    if previous is None:
        return {'first_publish': True, 'no_change': False, 'sections': [],
                'summary': '首次发布，无对比基线'}
    diff = diff_snapshots(previous.snapshot or {}, agent.snapshot())
    if diff['no_change']:
        diff['summary'] = '与线上版本无实质变化'
    return {'first_publish': False, 'base_version': previous.version, **diff}


def change_summary_of(diff: dict) -> str:
    """A8 发布历史里的"变更摘要"一行话"""
    if diff.get('first_publish'):
        return '首次发布'
    if diff.get('no_change'):
        return '与上一版无实质变化'
    parts = []
    for section in diff.get('sections') or []:
        labels = []
        for item in section['items']:
            if item['kind'] == 'text':
                labels.append(f'提示词{item["delta"]:+d} 字')
            elif item['kind'] == 'list':
                labels.append(f'{item["label"]}（{item["detail"]}）')
            elif item['kind'] == 'bool':
                labels.append(f'{item["label"]}→{item["new"]}')
            elif item['kind'] == 'value':
                labels.append(f'{item["label"]}：{item["old"] or "空"} → {item["new"] or "空"}')
            else:
                labels.append(item['detail'])
        parts.append(f'{section["title"]}：{"；".join(labels)}')
    text = '｜'.join(parts)
    return text if len(text) <= 200 else text[:197] + '…'


# ─── A8-1 版本对比 / A8-2 回滚 ───

def compare_versions(left: 'AgentVersion', right: 'AgentVersion') -> dict:
    """任选两版本对比：按版本号升序归一化为 左(旧) → 右(新)

    审计不写正文，调用方只记录 base/target 与分区名。
    """
    old, new = (left, right) if left.version <= right.version else (right, left)
    diff = diff_snapshots(old.snapshot or {}, new.snapshot or {})
    dependencies = (snapshot_dependencies(old.snapshot or {}, old.version)
                    + snapshot_dependencies(new.snapshot or {}, new.version))
    return {
        'base_version': old.version, 'target_version': new.version,
        'left_version': old.version, 'right_version': new.version,
        **diff, 'dependencies': dependencies,
        'unavailable_count': len([d for d in dependencies if d['level'] == 'blocking']),
        'warning_count': len([d for d in dependencies if d['level'] == 'warning']),
    }


def snapshot_dependencies(snapshot: dict, version: int | None = None,
                          agent=None) -> list:
    """A8：检查历史快照引用的对象此刻是否仍可用

    返回检查项列表（level=blocking 表示回滚后运行时不可用，必须阻断；
    level=warning 表示能力/可见范围降级，需管理员确认）。
    快照存的是部门名称、来源 slug、技能 slug，改名或删除后无法可靠还原，
    因此这里只做「存在性 + 可用性」判定，并明确写出「已不存在」。
    """
    from backend.apps.knowledge.models import KnowledgeBase
    from backend.apps.mcp.models import MCPServer
    from backend.apps.skills.models import Skill
    from backend.apps.users.models import Department

    items = []

    def add(level, code, section, message, field=None):
        row = _item(level, code, section, message, field)
        row['source'] = 'snapshot'
        if version is not None:
            row['version'] = version
        items.append(row)

    if agent is not None:
        name = (snapshot.get('name') or '').strip()
        if name and name != agent.name and \
                AgentProfile.objects.filter(name=name).exclude(pk=agent.pk).exists():
            add('blocking', 'ref_name_conflict', 'basic',
                f'该版本使用的名称「{name}」已被其他智能体占用，回滚会产生重名冲突', 'name')

    model_name = (snapshot.get('model_name') or '').strip()
    if model_name and not model_available(model_name):
        add('blocking', 'ref_model_unavailable', 'model',
            f'该版本使用的模型「{model_name}」当前不在已验证可用列表内，回滚后无法运行',
            'model_name')

    for mount in snapshot.get('mcp_servers') or []:
        slug = mount.get('server') or ''
        name = mount.get('server_name') or slug
        server = MCPServer.objects.filter(slug=slug).first() if slug else None
        if server is None:
            add('blocking', 'ref_server_missing', 'mcp_servers',
                f'该版本引用的工具来源「{name}」（{slug or "未知"}）已不存在，'
                f'回滚后这些工具无法恢复', 'mcp_servers')
            continue
        if not server.is_active or server.status == 'disabled':
            add('warning', 'ref_server_disabled', 'mcp_servers',
                f'该版本引用的工具来源「{server.name}」当前已停用，回滚后会话中会降级',
                'mcp_servers')
        elif server.status in SOURCE_UNVERIFIED_STATUSES:
            add('warning', 'ref_server_unverified', 'mcp_servers',
                f'该版本引用的工具来源「{server.name}」当前未验证或连接异常，'
                f'回滚后工具可能不可用', 'mcp_servers')
        if server.transport == 'stdio':
            add('warning', 'ref_server_stdio', 'mcp_servers',
                f'该版本引用的工具来源「{server.name}」是命令型来源，'
                f'会话中不注入其工具', 'mcp_servers')
        available = set(server.tools.filter(state='present', enabled=True)
                        .values_list('name', flat=True))
        gone = [t for t in (mount.get('tool_names') or []) if t not in available]
        if gone:
            add('warning', 'ref_tools_missing', 'mcp_servers',
                f'工具来源「{server.name}」中这些工具当前不存在或已关闭：'
                f'{"、".join(gone[:6])}，回滚后无法调用', 'mcp_servers')

    for slug in snapshot.get('skills') or []:
        skill = Skill.objects.filter(slug=slug).first()
        if skill is None:
            add('blocking', 'ref_skill_missing', 'skills',
                f'该版本引用的技能「{slug}」已不存在，回滚后技能能力无法恢复', 'skills')
        elif not skill.mountable:
            add('warning', 'ref_skill_disabled', 'skills',
                f'该版本引用的技能「{skill.name}」当前未启用，回滚后不会被注入',
                'skills')

    kb_ids = [int(i) for i in (snapshot.get('knowledge_base_ids') or [])
              if str(i).isdigit()]
    if kb_ids:
        found = set(KnowledgeBase.objects.filter(pk__in=kb_ids)
                    .values_list('pk', flat=True))
        gone = [i for i in kb_ids if i not in found]
        if gone:
            add('blocking', 'ref_kb_missing', 'knowledge_bases',
                f'该版本挂载的知识库已不存在：{gone}，回滚后无法检索', 'knowledge_base_ids')

    for name in snapshot.get('allowed_departments') or []:
        if not Department.objects.filter(name=name).exists():
            add('warning', 'ref_department_missing', 'visibility',
                f'该版本指定的可见部门「{name}」已不存在（可能已改名），'
                f'回滚后无法还原原可见范围，请确认后手工调整', 'allowed_departments')

    return items


def apply_snapshot(agent, snapshot: dict, actor) -> None:
    """把历史快照写回智能体的可编辑配置（不发布，发布由调用方完成）

    快照里的名称、部门名、来源/技能 slug 必须全部能解析，否则抛 ValueError；
    调用方应先跑 snapshot_dependencies 做阻断/告警判定。
    """
    from backend.apps.mcp.models import MCPServer
    from backend.apps.skills.models import Skill
    from backend.apps.users.models import Department

    scalar_fields = (
        'name', 'description', 'job_tags', 'icon', 'model_name', 'speech_tier',
        'retrieval_tier', 'knowledge_mode', 'followup_rewrite', 'enable_thinking',
        'prompt', 'knowledge_base_ids', 'default_preferences',
        'builtin_capabilities', 'permission_tier', 'allow_tool_names',
        'visibility', 'include_sub_departments',
    )
    for field in scalar_fields:
        if field in snapshot:
            value = snapshot[field]
            setattr(agent, field, list(value) if isinstance(value, list) else value)
    agent.updated_by = actor
    agent.save(update_fields=list(scalar_fields) + ['updated_by', 'updated_at'])

    mounts = snapshot.get('mcp_servers') or []
    servers = {s.slug: s for s in MCPServer.objects.filter(
        slug__in=[m.get('server') for m in mounts])}
    missing = [m.get('server') for m in mounts if m.get('server') not in servers]
    if missing:
        raise ValueError(f'工具来源已不存在：{missing}')
    AgentMCPServer.objects.filter(agent=agent).delete()
    for mount in mounts:
        AgentMCPServer.objects.create(
            agent=agent, server=servers[mount['server']],
            tool_names=list(mount.get('tool_names') or []))

    slugs = list(snapshot.get('skills') or [])
    skills = {s.slug: s for s in Skill.objects.filter(slug__in=slugs)}
    missing = [s for s in slugs if s not in skills]
    if missing:
        raise ValueError(f'技能已不存在：{missing}')
    AgentSkill.objects.filter(agent=agent).delete()
    for slug in slugs:
        AgentSkill.objects.create(agent=agent, skill=skills[slug])

    agent.allowed_departments.set(
        Department.objects.filter(name__in=snapshot.get('allowed_departments') or []))


# ─── 跨环境导出 / 导入（单级审批） ───

# 导出格式版本：字段口径变更时递增，导入端按版本提示
AGENT_EXPORT_VERSION = 1

# 导出字段：只含配置本身，不含任何密钥；模型 key、工具来源密钥需在目标环境重新填写
AGENT_EXPORT_FIELDS = (
    'name', 'description', 'job_tags', 'icon', 'model_name', 'speech_tier',
    'retrieval_tier', 'knowledge_mode', 'followup_rewrite', 'enable_thinking',
    'prompt', 'default_preferences', 'builtin_capabilities', 'permission_tier',
    'visibility', 'include_sub_departments',
)


def export_package(agent) -> dict:
    """单个智能体的导出包（换环境重建用，不是备份）

    引用对象一律按目标环境可解析的口径导出：工具来源与技能用 slug、
    知识库与部门用名称；密钥类字段不存在于智能体配置中。
    """
    from backend.apps.knowledge.models import KnowledgeBase

    kb_ids = [int(i) for i in (agent.knowledge_base_ids or []) if str(i).isdigit()]
    kb_map = {kb.pk: kb for kb in KnowledgeBase.objects.filter(pk__in=kb_ids)}
    payload = {field: getattr(agent, field) for field in AGENT_EXPORT_FIELDS}
    for field in ('job_tags', 'default_preferences', 'builtin_capabilities'):
        payload[field] = list(payload[field] or [])
    payload['owner_department'] = agent.owner_department.name if agent.owner_department else ''
    payload['mcp_mounts'] = [
        {'server_slug': m.server.slug, 'server_name': m.server.name,
         'tool_names': list(m.tool_names or [])}
        for m in agent.agentmcpserver_set.select_related('server').order_by('server__name')
    ]
    payload['skills'] = [{'slug': s.slug, 'name': s.name}
                         for s in agent.skills.all().order_by('name')]
    payload['knowledge_bases'] = [
        {'id': pk, 'name': kb_map[pk].name if pk in kb_map else ''} for pk in kb_ids]
    payload['allowed_departments'] = sorted(
        d.name for d in agent.allowed_departments.all())
    return {
        'format': 'lasso.agent',
        'version': AGENT_EXPORT_VERSION,
        'exported_at': timezone.now().isoformat(),
        'source': {'agent': agent.name, 'slug': agent.slug,
                   'status': agent.status, 'current_version': agent.current_version},
        'agent': payload,
        'note': ('不含任何密钥：模型与工具来源密钥需在目标环境重新填写；'
                 '导入产物为草稿，需在目标环境重新发布后才会对使用端生效'),
    }


def _import_issue(level, code, field, message) -> dict:
    return {'level': level, 'code': code, 'field': field, 'message': message}


def _import_enum(value, choices, default, label, warn):
    if value in choices:
        return value
    if value:
        warn('enum_invalid', label, f'{label}取值「{value}」不被支持，已回退为默认值')
    return default


def prepare_import(package: dict) -> dict:
    """校验导入包并解析为本环境可落库的配置（不写库）

    阻断项（level=blocking）＝提交/审批必须失败；告警项（warning）＝可以继续，
    但引用对象被跳过或降级，必须在提交页明确展示，不能静默丢弃。
    """
    from backend.apps.knowledge.models import KnowledgeBase
    from backend.apps.mcp.models import MCPServer
    from backend.apps.skills.models import Skill
    from backend.apps.users.models import Department

    warnings, blocking = [], []

    def warn(code, field, message):
        warnings.append(_import_issue('warning', code, field, message))

    def block(code, field, message):
        blocking.append(_import_issue('blocking', code, field, message))

    if not isinstance(package, dict):
        block('package_invalid', 'package', '导入包需为 JSON 对象')
        return {'blocking': blocking, 'warnings': warnings,
                'summary': {}, 'resolved': None}

    version = package.get('version')
    if version != AGENT_EXPORT_VERSION:
        block('version_unsupported', 'version',
              f'不支持的导出格式版本 {version}，当前仅支持 {AGENT_EXPORT_VERSION}')

    agent = package.get('agent')
    if not isinstance(agent, dict):
        block('agent_missing', 'agent', '导入包缺少 agent 对象')
        return {'blocking': blocking, 'warnings': warnings,
                'summary': {}, 'resolved': None}

    name = str(agent.get('name') or '').strip()
    if len(name) < 2 or len(name) > 30:
        block('name_invalid', 'name', '名称需为 2–30 个字')
    elif AgentProfile.objects.filter(name=name).exists():
        block('name_exists', 'name',
              f'目标环境已存在同名智能体「{name}」，导入不覆盖现有配置，请改名后重试')

    description = str(agent.get('description') or '').strip()
    if len(description) > 200:
        description = description[:200]
        warn('description_truncated', 'description', '用途描述超过 200 字，已截断')

    model_name = str(agent.get('model_name') or '').strip()
    if model_name and not model_available(model_name):
        warn('model_unavailable', 'model_name',
             f'模型「{model_name}」不在目标环境的已验证可用列表内，'
             f'导入后需先在系统设置完成验证并重新发布')

    speech_tier = _import_enum(agent.get('speech_tier'), SpeechTier.values,
                               SpeechTier.MID, '说话方式', warn)
    retrieval_tier = _import_enum(agent.get('retrieval_tier'), RetrievalTier.values,
                                  RetrievalTier.STANDARD, '查询次数上限', warn)
    knowledge_mode = _import_enum(agent.get('knowledge_mode'), KnowledgeMode.values,
                                  KnowledgeMode.ON_DEMAND, '检索调用策略', warn)
    permission_tier = _import_enum(agent.get('permission_tier'), PermissionTier.values,
                                   PermissionTier.WHITELIST, '授权档位', warn)
    visibility = _import_enum(agent.get('visibility'), Visibility.values,
                              Visibility.PRIVATE, '可见范围', warn)

    job_tags = [str(v).strip() for v in (agent.get('job_tags') or []) if str(v).strip()]
    default_preferences = [str(v).strip() for v in (agent.get('default_preferences') or [])
                           if str(v).strip()]
    builtin_values = {key for key, _ in BUILTIN_CAPABILITIES}
    builtin_capabilities = [v for v in (agent.get('builtin_capabilities') or [])
                            if v in builtin_values]
    dropped_builtin = [v for v in (agent.get('builtin_capabilities') or [])
                       if v not in builtin_values]
    if dropped_builtin:
        warn('builtin_unsupported', 'builtin_capabilities',
             f'目标环境不支持这些内置能力，已跳过：{"、".join(map(str, dropped_builtin))}')

    # ── 可见部门：按名称映射；指定部门可见时缺一个都阻断，避免静默缩小范围 ──
    dept_names = [str(n).strip() for n in (agent.get('allowed_departments') or [])
                  if str(n).strip()]
    dept_objs = list(Department.objects.filter(name__in=dept_names))
    found_dept_names = {d.name for d in dept_objs}
    missing_depts = [n for n in dept_names if n not in found_dept_names]
    if visibility == Visibility.DEPARTMENT:
        if not dept_objs:
            block('department_missing', 'allowed_departments',
                  f'可见范围为「指定部门」，但目标环境不存在这些部门：'
                  f'{"、".join(dept_names) or "未指定"}')
        elif missing_depts:
            block('department_missing', 'allowed_departments',
                  f'目标环境不存在这些部门：{"、".join(missing_depts)}，'
                  f'指定部门可见范围无法完整还原')
    elif missing_depts:
        warn('department_missing', 'allowed_departments',
             f'目标环境不存在这些部门，已跳过：{"、".join(missing_depts)}')

    owner_department = None
    owner_name = str(agent.get('owner_department') or '').strip()
    if owner_name:
        owner_department = Department.objects.filter(name=owner_name).first()
        if owner_department is None:
            warn('owner_department_missing', 'owner_department',
                 f'归属部门「{owner_name}」在目标环境不存在，已留空')

    # ── 工具来源：按 slug 映射；来源或工具缺失时逐项告警并跳过 ──
    mounts = []
    for raw in agent.get('mcp_mounts') or []:
        if not isinstance(raw, dict):
            continue
        slug = str(raw.get('server_slug') or raw.get('server') or '').strip()
        display = str(raw.get('server_name') or slug or '未命名来源')
        if not slug:
            warn('mcp_slug_missing', 'mcp_mounts', f'工具来源「{display}」缺少 slug，已跳过')
            continue
        server = MCPServer.objects.filter(slug=slug).first()
        if server is None:
            warn('mcp_missing', 'mcp_mounts',
                 f'工具来源「{display}」（{slug}）在目标环境不存在，已跳过该来源')
            continue
        if not server.is_active or server.status in ('disabled', 'egress_blocked'):
            warn('mcp_unavailable', 'mcp_mounts',
                 f'工具来源「{server.name}」在目标环境已停用或出网未开放，已跳过该来源')
            continue
        available = set(server.tools.filter(state='present', enabled=True)
                        .values_list('name', flat=True))
        requested = [str(n) for n in (raw.get('tool_names') or [])]
        gone = [n for n in requested if n not in available]
        if gone:
            warn('mcp_tools_missing', 'mcp_mounts',
                 f'工具来源「{server.name}」中这些工具不存在或已关闭，已跳过：'
                 f'{"、".join(gone)}')
        mounts.append({'server': server,
                       'tool_names': sorted({n for n in requested if n in available})})

    # ── 技能：按 slug 映射；不存在或不可挂载时跳过 ──
    skills = []
    for raw in agent.get('skills') or []:
        slug = str((raw or {}).get('slug') if isinstance(raw, dict) else raw or '').strip()
        display = str((raw or {}).get('name') if isinstance(raw, dict) else '') or slug
        if not slug:
            continue
        skill = Skill.objects.filter(slug=slug).first()
        if skill is None:
            warn('skill_missing', 'skills',
                 f'技能「{display}」（{slug}）在目标环境不存在，已跳过')
        elif not skill.mountable:
            warn('skill_unavailable', 'skills',
                 f'技能「{skill.name}」在目标环境未启用，已跳过')
        else:
            skills.append(skill)

    # ── 知识库：跨环境 ID 无意义，按名称映射；同名多条视为歧义跳过 ──
    kb_ids = []
    for raw in agent.get('knowledge_bases') or []:
        kb_name = str((raw or {}).get('name') if isinstance(raw, dict) else raw or '').strip()
        if not kb_name:
            continue
        rows = list(KnowledgeBase.objects.filter(name=kb_name)[:2])
        if not rows:
            warn('kb_missing', 'knowledge_bases',
                 f'知识库「{kb_name}」在目标环境不存在，已跳过')
        elif len(rows) > 1:
            warn('kb_ambiguous', 'knowledge_bases',
                 f'目标环境存在多个同名知识库「{kb_name}」，无法确定对应关系，已跳过')
        else:
            kb_ids.append(rows[0].pk)

    # ── 白名单：只保留映射后仍挂载的工具，避免指向不存在的工具 ──
    mounted_full = {tool_full_name(m['server'].slug, t)
                    for m in mounts for t in m['tool_names']}
    requested_allow = [str(v) for v in (agent.get('allow_tool_names') or [])]
    allow_tool_names = sorted({v for v in requested_allow if v in mounted_full})
    dropped_allow = [v for v in requested_allow if v not in mounted_full]
    if dropped_allow:
        warn('allow_tools_dropped', 'allow_tool_names',
             f'这些白名单工具未挂载成功，已移出白名单：{"、".join(dropped_allow[:8])}')

    summary = {
        'agent_name': name,
        'description': description,
        'model_name': model_name,
        'visibility': visibility,
        'source': package.get('source') or {},
        'counts': {
            'mcp_servers': len(mounts),
            'skills': len(skills),
            'knowledge_bases': len(kb_ids),
            'departments': len(dept_objs),
        },
    }
    resolved = None
    if not blocking:
        resolved = {
            'name': name,
            'description': description,
            'job_tags': job_tags,
            'icon': str(agent.get('icon') or ''),
            'model_name': model_name,
            'speech_tier': speech_tier,
            'retrieval_tier': retrieval_tier,
            'knowledge_mode': knowledge_mode,
            'followup_rewrite': bool(agent.get('followup_rewrite', True)),
            'enable_thinking': bool(agent.get('enable_thinking', False)),
            'prompt': str(agent.get('prompt') or ''),
            'default_preferences': default_preferences,
            'builtin_capabilities': builtin_capabilities,
            'permission_tier': permission_tier,
            'allow_tool_names': allow_tool_names,
            'visibility': visibility,
            'include_sub_departments': bool(agent.get('include_sub_departments', False)),
            'knowledge_base_ids': kb_ids,
            'owner_department': owner_department,
            'allowed_departments': dept_objs,
            'mcp_mounts': mounts,
            'skills': skills,
        }
    return {'blocking': blocking, 'warnings': warnings,
            'summary': summary, 'resolved': resolved}


def create_agent_from_import(resolved: dict, submitter, reviewer):
    """审批通过后落库：产物一律为草稿，不自动发布（A7 仍由目标环境执行）"""
    from django.db import IntegrityError

    try:
        with transaction.atomic():
            agent = AgentProfile.objects.create(
                name=resolved['name'],
                description=resolved['description'],
                job_tags=resolved['job_tags'],
                icon=resolved['icon'],
                model_name=resolved['model_name'],
                speech_tier=resolved['speech_tier'],
                retrieval_tier=resolved['retrieval_tier'],
                knowledge_mode=resolved['knowledge_mode'],
                followup_rewrite=resolved['followup_rewrite'],
                enable_thinking=resolved['enable_thinking'],
                prompt=resolved['prompt'],
                default_preferences=resolved['default_preferences'],
                builtin_capabilities=resolved['builtin_capabilities'],
                permission_tier=resolved['permission_tier'],
                allow_tool_names=resolved['allow_tool_names'],
                visibility=resolved['visibility'],
                include_sub_departments=resolved['include_sub_departments'],
                knowledge_base_ids=resolved['knowledge_base_ids'],
                owner_department=resolved['owner_department'],
                status=AgentStatus.DRAFT,
                created_by=submitter,
                updated_by=reviewer,
            )
            for mount in resolved['mcp_mounts']:
                AgentMCPServer.objects.create(agent=agent, server=mount['server'],
                                              tool_names=mount['tool_names'])
            for skill in resolved['skills']:
                AgentSkill.objects.create(agent=agent, skill=skill)
            agent.allowed_departments.set(resolved['allowed_departments'])
    except IntegrityError:
        raise ValidationError({'detail': '导入落库失败：目标环境已存在同名智能体或标识冲突'})
    return agent


# ─── 运行时配置（供会话与试聊读取，A7/7.3 用已发布快照） ───

def pending_changes(agent) -> bool:
    """草稿修改尚未发布：进行中会话仍用线上快照（7.3）"""
    previous = AgentVersion.objects.filter(agent=agent).order_by('-version').first()
    if previous is None:
        return agent.current_version == 0
    return bool(AgentVersion.diff_of(previous.snapshot, agent.snapshot()))


def runtime_config(agent, version: AgentVersion | None = None) -> dict:
    """会话运行时装配所需的取值（档位→数值在这里换算，界面无数值输入项）"""
    snap = version.snapshot if version else agent.snapshot()
    tier = snap.get('speech_tier', SpeechTier.MID)
    return {
        'slug': snap.get('slug'),
        'name': snap.get('name'),
        'model': snap.get('model_name'),
        'system_prompt': snap.get('prompt', ''),
        'speech_tier': tier,
        **SPEECH_PRESETS.get(tier, SPEECH_PRESETS[SpeechTier.MID]),
        'retrieval_tier': snap.get('retrieval_tier'),
        'max_tool_rounds': RETRIEVAL_PRESETS.get(snap.get('retrieval_tier'),
                                                RETRIEVAL_PRESETS[RetrievalTier.STANDARD]),
        'knowledge_mode': snap.get('knowledge_mode') or 'on_demand',
        'followup_rewrite': snap.get('followup_rewrite', True),
        'enable_thinking': snap.get('enable_thinking', False),
        'permission_tier': snap.get('permission_tier'),
        'allow_tool_names': list(snap.get('allow_tool_names') or []),
        'builtin_capabilities': list(snap.get('builtin_capabilities') or []),
        'knowledge_base_ids': list(snap.get('knowledge_base_ids') or []),
        'skills': list(snap.get('skills') or []),
        'mcp_servers': [{'server': m['server'], 'tool_names': list(m['tool_names'])}
                        for m in (snap.get('mcp_servers') or [])],
        'snapshot_version': version.version if version else None,
    }


def mount_options_for(agent, user):
    """装配页四个选择器的候选（来源可展开逐个勾选工具、技能灰置带原因）"""
    from backend.apps.mcp.models import MCPServer
    from backend.apps.skills.permissions import visible_skill_query
    from backend.apps.skills.models import Skill

    servers = []
    mq = MCPServer.objects.filter(is_active=True).exclude(
        status__in=['disabled', 'egress_blocked'])
    if not (user.is_staff or user.is_superuser):
        from backend.apps.mcp.views import visible_server_query

        mq = mq.filter(visible_server_query(user)).distinct()
    mounted = {row.server_id: row.tool_names
               for row in agent.agentmcpserver_set.all()} if agent.pk else {}
    for server in mq.prefetch_related('tools'):
        chosen = mounted.get(server.pk, [])
        servers.append({
            'id': server.pk, 'name': server.name, 'slug': server.slug,
            'status': server.status, 'status_display': server.get_status_display(),
            'is_builtin': server.is_builtin,
            'verified': server.status == 'healthy',
            'warning': '' if server.status == 'healthy' else
                       ('该来源未验证，挂载后发布会警示' if server.status == 'unknown'
                        else f'该来源异常：{server.last_error or "连接失败"}，会话会降级'),
            'used_by_agents': server.agentmcpserver_set.count(),
            'checked': bool(chosen),
            'tools': [{'name': t.name, 'full_name': t.full_name,
                       'display_name': tool_display_name(server.name, t.name),
                       'description': t.description, 'enabled': t.enabled,
                       'state': t.state,
                       'selectable': t.enabled and t.state == 'present',
                       'reason': '' if (t.enabled and t.state == 'present') else
                                 ('来源侧已关闭' if t.state == 'present' else '工具已失效'),
                       'checked': t.name in chosen}
                      for t in server.tools.filter(state='present').order_by('name')],
        })

    skills = []
    sq = Skill.objects.all().prefetch_related('applicable_departments')
    if not (user.is_staff or user.is_superuser):
        sq = sq.filter(visible_skill_query(user)).distinct()
    mounted_skills = set(agent.skills.values_list('pk', flat=True)) if agent.pk else set()
    for skill in sq.order_by('name'):
        skills.append({
            'id': skill.pk, 'name': skill.name, 'slug': skill.slug,
            'description': skill.description, 'status': skill.status,
            'status_display': skill.get_status_display(),
            'selectable': skill.mountable,
            'reason': '' if skill.mountable else '未启用的技能不可挂载',
            'checked': skill.pk in mounted_skills,
            'requires': list(skill.requires or []),
            # C5：装配页要能看出这项技能对谁生效
            'scope_mode': skill.scope_mode,
            'scope_mode_display': skill.get_scope_mode_display(),
            'scope_departments': list(skill.applicable_departments.values_list('name', flat=True)),
            'scope_include_sub_departments': skill.scope_include_sub_departments,
            'applies_to_me': skill.applies_to(user),
        })

    from backend.apps.knowledge.models import KnowledgeBase

    kq = KnowledgeBase.objects.all()
    if not (user.is_staff or user.is_superuser):
        from backend.apps.knowledge.models import visible_kb_query

        kq = kq.filter(visible_kb_query(user))
    kb_ids = set(int(i) for i in (agent.knowledge_base_ids or []) if str(i).isdigit())
    knowledges = [{'id': kb.pk, 'name': kb.name, 'document_count': kb.document_count,
                   'empty': not kb.document_count, 'checked': kb.pk in kb_ids}
                  for kb in kq.order_by('-updated_at')]

    return {
        'mcp_servers': servers,
        'skills': skills,
        'knowledge_bases': knowledges,
        'builtin_capabilities': [
            {'value': key, 'label': label,
             'checked': key in (agent.builtin_capabilities or [])}
            for key, label in BUILTIN_CAPABILITIES],
    }


def status_summary(user=None):
    """列表页顶部统计 + 空状态引导（A1）"""
    qs = AgentProfile.objects.all()
    if user is not None and not (user.is_staff or user.is_superuser):
        qs = qs.filter(visible_agent_query(user)).distinct()
    out = {key: 0 for key, _ in AgentStatus.choices}
    for value in qs.values_list('status', flat=True):
        out[value] = out.get(value, 0) + 1
    out['total'] = sum(v for k, v in out.items() if k != 'total')
    out['builtin'] = AgentProfile.objects.filter(is_builtin=True).count()
    out['empty'] = out['total'] == 0
    if out['empty']:
        out['guide'] = ('还没有智能体。可先从内置智能体复制一个再改配置，'
                        '或新建一个空白智能体')
    return out
