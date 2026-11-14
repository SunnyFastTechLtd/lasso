"""外部条目的落库草稿与单级审批

口径（与 AgentImportRequest 一致）：
- 提交不生效：这里只生成"归一化草稿 + 风险标记"；
- 管理员且非提交人审批通过后才真正建 MCPServer / Skill；
- MCP 一律 `status=unknown`、`enabled_tools=[]`；技能一律 `pending`，未审核不进运行时。
"""
import logging
import re

from django.db import transaction
from django.utils import timezone

from backend.apps.mcp.models import MCPServer
from backend.apps.nuwa import net
from backend.apps.nuwa.models import NuwaImportRequest
from backend.apps.skills.models import MAX_BODY_CHARS, Skill, SkillSource, SkillStatus

logger = logging.getLogger(__name__)


def build_mcp_draft(item) -> tuple:
    """市场条目 → MCPServer 落库参数 + 风险标记"""
    payload = item.install_payload or {}
    risks = []
    if not item.license:
        risks.append('license_unknown')
    if item.trust_level != 'trusted':
        risks.append('unverified_source')
    draft = {
        'name': (item.title or item.external_id)[:128],
        'description': (item.summary or '')[:200],
        'transport': payload.get('type') or item.transport,
        'url': payload.get('url', ''),
        'egress_domains': [item.egress_host] if item.egress_host else [],
        'requires_egress': bool(item.requires_egress),
        'command': payload.get('command', ''),
        'args': list(payload.get('args') or []),
        'env': list(payload.get('env') or []),
        'source_url': item.homepage or item.repository_url,
        'market_item_id': item.pk,
    }
    if draft['transport'] == 'http_mcp' and not draft['url']:
        risks.append('missing_url')
    if draft['transport'] == 'stdio' and not draft['command']:
        risks.append('missing_command')
    if any(env.get('required') and not env.get('default') for env in draft['env']):
        risks.append('requires_secrets')
    if item.requires_egress:
        risks.append('runtime_egress')
    return draft, risks


def build_skill_draft(item) -> tuple:
    """市场条目 → Skill 落库参数；正文在提交时抓取，审批页审的就是这份正文"""
    payload = item.install_payload or {}
    risks = []
    if not item.license:
        risks.append('license_unknown')
    if item.trust_level != 'trusted':
        risks.append('unverified_source')
    body = ''
    url = payload.get('skill_md_url') or ''
    if url:
        try:
            body = net.request_text(url, timeout=net.SKILL_FILE_TIMEOUT, retries=1,
                                    max_bytes=64 * 1024)
        except net.RemoteError as exc:
            risks.append('body_unavailable')
            logger.info('技能正文抓取失败 %s: %s', url, exc.reason)
    else:
        risks.append('body_unavailable')
    if body and len(body) > MAX_BODY_CHARS:
        risks.append('body_too_long')
    if body and 'scripts/' in str(item.raw.get('body_preview', '')):
        risks.append('contains_scripts')
    draft = {
        'name': (item.title or item.external_id)[:128],
        'description': (item.summary or '')[:200],
        'body': body,
        'entry_file': 'SKILL.md',
        'repo': payload.get('repo', ''),
        'path': payload.get('path', ''),
        'source_url': item.homepage or '',
        'license': item.license,
        'market_item_id': item.pk,
    }
    return draft, risks


def submit(item, user) -> NuwaImportRequest:
    """同一用户对同一条目的待审批申请只保留一条，避免重复提交"""
    existing = NuwaImportRequest.objects.filter(
        market_item=item, requested_by=user, status='pending').first()
    if existing:
        return existing
    if item.kind == 'mcp':
        draft, risks = build_mcp_draft(item)
        kind = 'mcp_server'
    else:
        draft, risks = build_skill_draft(item)
        kind = 'skill'
    return NuwaImportRequest.objects.create(
        kind=kind, market_item=item, requested_by=user,
        draft_payload=draft, risk_flags=risks,
    )


def _unique_name(model, base: str) -> str:
    name = (base or '未命名')[:120]
    index = 2
    while model.objects.filter(name=name).exists():
        suffix = f'-{index}'
        name = f'{(base or "未命名")[:120 - len(suffix)]}{suffix}'
        index += 1
    return name


def _clean_secret_env(env_list) -> dict:
    """只保留键名合法的环境变量；值先落空，审批后由管理员在来源页补填"""
    values = {}
    for env in env_list or []:
        key = str(env.get('name') or '')
        if re.match(r'^[A-Z][A-Z0-9_]{0,63}$', key):
            values[key] = str(env.get('default') or '')
    return values


@transaction.atomic
def approve(request_obj: NuwaImportRequest, actor, note: str = '') -> dict:
    if request_obj.status != 'pending':
        raise ValueError('该申请已处理')
    if request_obj.requested_by_id and request_obj.requested_by_id == actor.pk:
        raise PermissionError('提交人不能审批自己的申请')

    draft = request_obj.draft_payload or {}
    result = {}
    if request_obj.kind == 'mcp_server':
        server = MCPServer.objects.create(
            name=_unique_name(MCPServer, draft.get('name') or '外部工具来源'),
            description=(draft.get('description') or '')[:200],
            transport=draft.get('transport') or 'http_mcp',
            url=draft.get('url') or '',
            egress_domains=list(draft.get('egress_domains') or []),
            requires_egress=bool(draft.get('requires_egress')),
            command=draft.get('command') or '',
            args=list(draft.get('args') or []),
            enabled_tools=[],
            status='unknown',
            created_by=actor,
        )
        env_values = _clean_secret_env(draft.get('env'))
        if env_values:
            server.set_secrets({'headers': {}, 'env': env_values})
            server.save(update_fields=['secrets_enc'])
        # 来源建好后立即测一次：失败只记状态，不阻塞审批
        try:
            from backend.apps.mcp import services as mcp_services

            mcp_services.test_server(server, actor=actor)
        except Exception as exc:
            logger.info('女娲导入来源首次检测失败 %s: %s', server.pk, exc)
        result = {'mcp_server_id': server.pk, 'name': server.name,
                  'tools_enabled': 0}
    elif request_obj.kind == 'skill':
        body = draft.get('body') or ''
        if not body.strip():
            raise ValueError('技能正文为空（提交时抓取失败），无法落库')
        skill = Skill.objects.create(
            name=_unique_name(Skill, draft.get('name') or '外部技能'),
            description=(draft.get('description') or '')[:200],
            source=SkillSource.REPO,
            entry_file='SKILL.md',
            body=body,
            status=SkillStatus.INVALID,
            created_by=actor,
        )
        status_value = skill.submit()
        if status_value == SkillStatus.INVALID:
            errors = list(skill.validation_errors or [])
            skill.delete()
            raise ValueError('技能正文未通过校验：' + ('；'.join(errors[:3]) or '未知原因'))
        result = {'skill_id': skill.pk, 'name': skill.name, 'status': skill.status}
    else:
        raise ValueError('未知的申请类型')

    request_obj.status = 'approved'
    request_obj.reviewed_by = actor
    request_obj.reviewed_at = timezone.now()
    request_obj.review_note = (note or '')[:200]
    request_obj.result = result
    request_obj.save(update_fields=['status', 'reviewed_by', 'reviewed_at',
                                    'review_note', 'result'])
    from backend.apps.users.models import log_audit

    log_audit(actor=actor, action='nuwa_import_approved',
              target=f'{request_obj.get_kind_display()}#{request_obj.pk}',
              detail=f'落库：{result}')
    return result


def reject(request_obj: NuwaImportRequest, actor, note: str = '') -> NuwaImportRequest:
    if request_obj.status != 'pending':
        raise ValueError('该申请已处理')
    if request_obj.requested_by_id and request_obj.requested_by_id == actor.pk:
        raise PermissionError('提交人不能审批自己的申请')
    request_obj.status = 'rejected'
    request_obj.reviewed_by = actor
    request_obj.reviewed_at = timezone.now()
    request_obj.review_note = (note or '')[:200]
    request_obj.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'review_note'])
    from backend.apps.users.models import log_audit

    log_audit(actor=actor, action='nuwa_import_rejected',
              target=f'{request_obj.get_kind_display()}#{request_obj.pk}',
              detail=(note or '')[:200])
    return request_obj
