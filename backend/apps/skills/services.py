"""技能规范的服务层（C1–C6 的编排逻辑）

职责边界：模型层承载状态机与校验规则，本模块负责——
1. DB → 磁盘镜像（AgentScope 的 LocalSkillLoader 只认目录，正文与附件以数据库为唯一真源）
2. 技能包附件校验、风险扫描、确认指纹（C1 + 决策 10）
3. 版本快照、正文差异与回滚（C4）
4. 挂载引用检查（C6）与审核队列口径（C3）

镜像只写"审核通过版本"（最新 SkillVersion），编辑中的待审核稿件不进磁盘，
因此 C4 的"原启用版本继续生效直至新版通过"由镜像层直接保证。
"""
import difflib
import hashlib
import json
import logging
import re
import shutil
from pathlib import PurePosixPath

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from backend.apps.skills.models import (
    Skill, SkillBlob, SkillFile, SkillStatus, SkillVersion, SkillVersionFile,
)
from backend.apps.skills.scanner import RULESET_VERSION, scan_text

logger = logging.getLogger(__name__)

# ─── 技能包附件边界（决策 10）───
SKILL_PACKAGE_SETTINGS_KEY = 'skill_package_config'
DEFAULT_ALLOWED_EXTENSIONS = ['.md', '.txt', '.json', '.yaml', '.csv']
DENIED_EXTENSIONS = {
    '.exe', '.dll', '.so', '.dylib', '.bat', '.cmd', '.com', '.msi', '.scr',
    '.jar', '.class', '.bin', '.zip', '.rar', '.7z', '.tar', '.gz', '.bz2',
    '.docx', '.xlsx', '.pptx', '.pdf', '.png', '.jpg', '.jpeg', '.gif', '.svg',
    '.webp', '.ico', '.woff', '.woff2', '.ttf', '.otf', '.mp3', '.mp4', '.mov',
}
MAX_ATTACHMENT_BYTES = 200 * 1024
MAX_ATTACHMENTS = 20
MAX_PACKAGE_BYTES = 2 * 1024 * 1024
MAX_REL_PATH_LENGTH = 255
MAX_REL_PATH_DEPTH = 3
_EXTENSION_RE = re.compile(r'^\.[a-z0-9]{1,10}$')


class SkillPackageError(ValueError):
    """结构性错误（400，不给风险确认入口）"""


class RiskConfirmRequired(Exception):
    """命中高危，需要管理员显式确认（422）"""

    def __init__(self, hits, fingerprint):
        self.hits = hits
        self.fingerprint = fingerprint
        super().__init__('risk_ack_required')


# ─── 附件类型配置 ───

def normalize_extensions(values) -> list:
    """把任意输入清洗成合法扩展名列表；非法项直接忽略"""
    cleaned, seen = [], set()
    for raw in values or []:
        value = str(raw or '').strip().lower()
        if not value:
            continue
        if not value.startswith('.'):
            value = '.' + value
        if not _EXTENSION_RE.match(value) or value in DENIED_EXTENSIONS:
            continue
        if value not in seen:
            seen.add(value)
            cleaned.append(value)
    return cleaned


def invalid_extensions(values) -> list:
    """返回配置里不可接受的扩展名（用于设置接口报错）"""
    bad = []
    for raw in values or []:
        value = str(raw or '').strip().lower()
        if not value:
            continue
        if not value.startswith('.'):
            value = '.' + value
        if not _EXTENSION_RE.match(value) or value in DENIED_EXTENSIONS:
            bad.append(value)
    return bad


def allowed_extensions() -> list:
    """读取系统设置；缺失或损坏时回退出厂保守集"""
    from backend.apps.settings.models import SystemSetting

    raw = SystemSetting.get_value(SKILL_PACKAGE_SETTINGS_KEY, '')
    if raw:
        try:
            data = json.loads(raw)
            values = data.get('allowed_extensions') if isinstance(data, dict) else data
            cleaned = normalize_extensions(values)
            if cleaned:
                return cleaned
        except (TypeError, ValueError):
            logger.warning('技能包附件类型配置损坏，使用出厂默认值')
    return list(DEFAULT_ALLOWED_EXTENSIONS)


# ─── 附件结构校验 ───

def normalize_rel_path(rel_path: str) -> str:
    raw = str(rel_path or '').replace('\\', '/').strip()
    if not raw:
        raise SkillPackageError('文件路径不能为空')
    if raw.startswith('/') or re.match(r'^[A-Za-z]:', raw):
        raise SkillPackageError('不允许绝对路径')
    pure = PurePosixPath(raw)
    if pure.is_absolute() or any(part in ('', '.', '..') for part in pure.parts):
        raise SkillPackageError('路径不能包含 .. 或空片段')
    if len(raw) > MAX_REL_PATH_LENGTH:
        raise SkillPackageError(f'路径长度不能超过 {MAX_REL_PATH_LENGTH} 个字符')
    if len(pure.parts) > MAX_REL_PATH_DEPTH:
        raise SkillPackageError(f'路径层级不能超过 {MAX_REL_PATH_DEPTH} 层')
    if any(part.startswith('.') for part in pure.parts):
        raise SkillPackageError('不允许隐藏文件或隐藏目录')
    if raw.lower() == 'skill.md':
        raise SkillPackageError('SKILL.md 是技能主文件，请通过正文编辑更新')
    return str(pure)


def validate_attachments(uploads, existing_paths=(), existing_bytes=0,
                         existing_count=0) -> dict:
    """校验附件结构；返回 errors / files / total_bytes。

    uploads: [(rel_path, raw_bytes)]
    结构性错误一次性全部返回，调用方直接 400，不进入风险确认。
    """
    errors = []
    files = []
    paths = {str(p) for p in existing_paths}
    total = int(existing_bytes or 0)
    count = int(existing_count or 0)
    extensions = set(allowed_extensions())

    for rel_path, raw in uploads:
        shown = str(rel_path or '')
        try:
            path = normalize_rel_path(rel_path)
        except SkillPackageError as exc:
            errors.append({'file': shown, 'code': 'path_invalid', 'message': str(exc)})
            continue

        suffix = PurePosixPath(path).suffix.lower()
        if suffix in DENIED_EXTENSIONS:
            errors.append({'file': shown, 'code': 'type_denied',
                           'message': f'不允许上传 {suffix} 类型文件'})
            continue
        if suffix not in extensions:
            errors.append({'file': shown, 'code': 'type_not_allowed',
                           'message': f'当前允许的类型不含 {suffix or "无扩展名"}'})
            continue
        if path in paths:
            errors.append({'file': shown, 'code': 'duplicate_path',
                           'message': '同名文件已存在，请先删除再上传'})
            continue
        if len(raw) > MAX_ATTACHMENT_BYTES:
            errors.append({'file': shown, 'code': 'file_too_large',
                           'message': f'单文件不能超过 {MAX_ATTACHMENT_BYTES // 1024}KB'})
            continue
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            errors.append({'file': shown, 'code': 'not_text',
                           'message': '只接受 UTF-8 文本文件'})
            continue
        if '\x00' in text:
            errors.append({'file': shown, 'code': 'not_text',
                           'message': '疑似二进制文件，只接受纯文本'})
            continue

        paths.add(path)
        count += 1
        total += len(raw)
        files.append({
            'rel_path': path,
            'raw': raw,
            'text': text,
            'size': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(),
        })

    if count > MAX_ATTACHMENTS:
        errors.append({'file': '', 'code': 'too_many_files',
                       'message': f'附件数量不能超过 {MAX_ATTACHMENTS} 个'})
    if total > MAX_PACKAGE_BYTES:
        errors.append({'file': '', 'code': 'package_too_large',
                       'message': f'技能包总大小不能超过 {MAX_PACKAGE_BYTES // (1024 * 1024)}MB'})
    return {'errors': errors, 'files': files, 'total_bytes': total, 'count': count}


# ─── 风险扫描与确认指纹 ───

def package_fingerprint(body: str, file_hashes) -> str:
    """正文 + 附件指纹 + 规则集版本，任何一项变化都会让旧确认失效"""
    body_hash = hashlib.sha256((body or '').encode('utf-8')).hexdigest()
    manifest = '|'.join(f'{path}:{digest}' for path, digest in sorted(file_hashes or []))
    payload = f'{RULESET_VERSION}|{body_hash}|{manifest}'
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def scan_package(body: str, files) -> dict:
    """扫描正文与附件；files: [{'rel_path','text','sha256'}]"""
    hits = scan_text(body or '', 'SKILL.md')['high_risk']
    warnings = list(scan_text(body or '', 'SKILL.md')['warnings'])
    for item in files or []:
        result = scan_text(item['text'], item['rel_path'])
        hits.extend(result['high_risk'])
        warnings.extend(result['warnings'])
    fingerprint = package_fingerprint(body, [(f['rel_path'], f['sha256']) for f in files or []])
    return {'hits': hits, 'warnings': warnings, 'fingerprint': fingerprint}


def prospective_attachments(skill: Skill, extra=None, exclude_ids=()) -> list:
    """当前编辑稿附件（可选追加本次上传、排除待删除项）"""
    items = []
    for item in skill.files.select_related('blob').all():
        if item.entry or item.rel_path == skill.entry_file or item.blob is None:
            continue
        if item.pk in set(exclude_ids or ()):
            continue
        try:
            text = bytes(item.blob.content).decode('utf-8-sig')
        except UnicodeDecodeError:
            text = ''
        items.append({'rel_path': item.rel_path, 'text': text,
                      'sha256': item.sha256 or item.blob.sha256})
    items.extend(extra or [])
    return items


def ack_fields_for(hits, files, actor, fingerprint) -> dict:
    """确认放行时需要落库的字段"""
    return {
        'risk_ack_at': timezone.now(),
        'risk_ack_by': actor,
        'risk_ack_fingerprint': fingerprint,
        'risk_ack_rules': [
            {'rule': h.get('rule'), 'rule_version': h.get('rule_version'),
             'ruleset_version': h.get('ruleset_version'),
             'file': h.get('file'), 'line': h.get('line')}
            for h in hits
        ],
        'risk_ack_files': sorted({h.get('file') for h in hits if h.get('file')}),
        'risk_ack_ruleset_version': RULESET_VERSION,
    }


def risk_ack_valid(skill: Skill, fingerprint: str, ack_requested: bool,
                   ack_fingerprint: str = '') -> tuple:
    """判断本次提交是否构成有效的确认放行，返回 (ok, reason)"""
    if not ack_requested:
        return False, 'risk_ack_required'
    if not ack_fingerprint or ack_fingerprint != fingerprint:
        return False, 'risk_ack_stale'
    return True, ''


def log_risk_ack(skill: Skill, actor, hits) -> None:
    from backend.apps.users.models import log_audit

    rules = sorted({f"{h.get('rule')}@{h.get('rule_version')}" for h in hits})
    files = sorted({h.get('file') for h in hits if h.get('file')})
    log_audit(
        actor=actor,
        action='skill_risk_ack',
        target=f'技能 {skill.name}',
        detail=(f'规则集={RULESET_VERSION}；规则={",".join(rules)}；'
                f'文件={",".join(files)}'),
    )


# ─── DB → 磁盘镜像 ───

def _mirror_text(skill: Skill, body: str) -> str:
    """SKILL.md 内容：YAML frontmatter + 生效正文（供加载器解析名称与适用说明）"""
    desc = (skill.description or '').replace('\n', ' ').strip()
    return (
        '---\n'
        f'name: {skill.name}\n'
        f'description: {desc}\n'
        f'slug: {skill.slug}\n'
        '---\n\n'
        f'{body}\n'
    )


def sync_mirror(skill: Skill) -> str:
    """把最新审核通过版本（正文 + 附件）镜像到 SKILLS_MIRROR_DIR/{slug}/。

    只有"仍可挂载且已有审核通过版本"的技能会落盘；整目录重建，确保被删除的
    附件不会残留在运行目录里。待审核编辑稿不进磁盘。
    """
    entry_name = skill.entry_file or 'SKILL.md'
    target_dir = skill.mirror_dir
    if not (skill.mountable and skill.runtime_body):
        remove_mirror(skill)
        return ''
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    target_dir.mkdir(parents=True, exist_ok=False)
    path = target_dir / entry_name
    path.write_text(_mirror_text(skill, skill.runtime_body), encoding='utf-8')

    version = skill.latest_version
    if version is not None:
        for item in version.files.select_related('blob').all():
            dest = target_dir / item.rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(bytes(item.blob.content))
    return str(path)


def remove_mirror(skill: Skill) -> bool:
    """删除技能的镜像目录；目录不存在视为成功"""
    target_dir = skill.mirror_dir
    if not target_dir.exists():
        return False
    shutil.rmtree(target_dir, ignore_errors=True)
    return not target_dir.exists()


def mirror_root():
    """镜像根目录（不存在则创建，供加载器扫描）"""
    root = settings.SKILLS_MIRROR_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def rebuild_all_mirror() -> dict:
    """全量重建镜像（启动期/迁移后自愈用），返回统计"""
    mirrored, removed = [], []
    for skill in Skill.objects.all():
        if sync_mirror(skill):
            mirrored.append(skill.slug)
        elif remove_mirror(skill):
            removed.append(skill.slug)
    return {'mirrored': len(mirrored), 'removed': len(removed),
            'slugs': mirrored, 'removed_slugs': removed}


# ─── C4 版本差异与回滚 ───

def version_files_map(version: SkillVersion) -> dict:
    return {item.rel_path: item for item in version.files.all()}


def compare_versions(left: SkillVersion, right: SkillVersion) -> dict:
    """正文行级 diff + 附件增删改清单"""
    old_lines = (left.body or '').splitlines()
    new_lines = (right.body or '').splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    rows, added, removed = [], 0, 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            for offset in range(i2 - i1):
                rows.append({'type': 'equal', 'text': old_lines[i1 + offset],
                             'left_no': i1 + offset + 1, 'right_no': j1 + offset + 1})
        elif tag == 'delete':
            for offset in range(i2 - i1):
                rows.append({'type': 'del', 'text': old_lines[i1 + offset],
                             'left_no': i1 + offset + 1, 'right_no': None})
                removed += 1
        elif tag == 'insert':
            for offset in range(j2 - j1):
                rows.append({'type': 'add', 'text': new_lines[j1 + offset],
                             'left_no': None, 'right_no': j1 + offset + 1})
                added += 1
        else:  # replace
            for offset in range(i2 - i1):
                rows.append({'type': 'del', 'text': old_lines[i1 + offset],
                             'left_no': i1 + offset + 1, 'right_no': None})
                removed += 1
            for offset in range(j2 - j1):
                rows.append({'type': 'add', 'text': new_lines[j1 + offset],
                             'left_no': None, 'right_no': j1 + offset + 1})
                added += 1

    old_files = version_files_map(left)
    new_files = version_files_map(right)
    added_files = [{'path': p, 'size': new_files[p].size}
                   for p in sorted(new_files.keys() - old_files.keys())]
    removed_files = [{'path': p, 'size': old_files[p].size}
                     for p in sorted(old_files.keys() - new_files.keys())]
    changed_files = [{'path': p, 'old_size': old_files[p].size, 'new_size': new_files[p].size}
                     for p in sorted(old_files.keys() & new_files.keys())
                     if old_files[p].sha256 != new_files[p].sha256]
    summary = (f'正文 +{added}/-{removed} 行；文件新增 {len(added_files)}、'
               f'移除 {len(removed_files)}、变更 {len(changed_files)}')
    return {
        'left_version': left.version,
        'right_version': right.version,
        'body': {'stats': {'added': added, 'removed': removed,
                           'unchanged': len(old_lines) - removed},
                 'rows': rows},
        'files': {'added': added_files, 'removed': removed_files,
                  'changed': changed_files,
                  'unchanged_count': len(old_files.keys() & new_files.keys())
                  - len(changed_files)},
        'summary': summary,
    }


def rollback_to(skill: Skill, target: SkillVersion, actor, ack_requested=False,
                ack_fingerprint='') -> dict:
    """回滚到历史版本：生成新版本，不覆盖历史。

    历史版本曾审核通过，但仍按当前规则集重扫；规则集升级后新增命中时，
    必须由操作人重新确认，不能借回滚绕过风险确认。
    """
    files = []
    for item in target.files.select_related('blob').all():
        try:
            text = bytes(item.blob.content).decode('utf-8-sig')
        except UnicodeDecodeError:
            text = ''
        files.append({'rel_path': item.rel_path, 'text': text,
                      'sha256': item.sha256 or item.blob.sha256})
    result = scan_package(target.body, files)
    hits, fingerprint = result['hits'], result['fingerprint']
    inherited = bool(
        hits and target.risk_ack_at
        and target.risk_ack_ruleset_version == RULESET_VERSION
    )
    if hits and not inherited:
        ok, _ = risk_ack_valid(skill, fingerprint, ack_requested, ack_fingerprint)
        if not ok:
            raise RiskConfirmRequired(hits, fingerprint)

    with transaction.atomic():
        Skill.objects.select_for_update().filter(pk=skill.pk).exists()
        skill.body = target.body or ''
        skill.char_count = len(skill.body)
        skill.clear_risk_ack()
        skill.files.exclude(entry=True).delete()
        for item in target.files.select_related('blob').all():
            SkillFile.objects.create(
                skill=skill, rel_path=item.rel_path, entry=False,
                size=item.size, sha256=item.sha256, blob=item.blob,
            )
        if hits:
            if inherited:
                skill.risk_ack_at = target.risk_ack_at
                skill.risk_ack_by = target.risk_ack_by
                skill.risk_ack_fingerprint = fingerprint
                skill.risk_ack_rules = list(target.risk_ack_rules or [])
                skill.risk_ack_files = list(target.risk_ack_files or [])
                skill.risk_ack_ruleset_version = target.risk_ack_ruleset_version
            else:
                for key, value in ack_fields_for(hits, files, actor, fingerprint).items():
                    setattr(skill, key, value)
                log_risk_ack(skill, actor, hits)
        skill.save()
        skill.approve(actor, change_summary=f'回滚至 v{target.version}')
        sync_mirror(skill)

    from backend.apps.users.models import log_audit

    new_version = skill.current_version
    name = getattr(actor, 'username', '') or '-'
    log_audit(
        actor=actor,
        action='skill_rollback',
        target=f'技能:{skill.name}',
        detail=(f'技能标识 {skill.slug}；回滚至 v{target.version}，'
                f'生成 v{new_version}；附件 {len(target.files.all())} 个；'
                f'操作人 {name}'),
    )
    return {'rolled_back_from': target.version, 'new_version': new_version,
            'files': len(target.files.all())}


# ─── C6 挂载引用检查 ───

def skill_references(skill: Skill) -> list:
    """被哪些智能体挂载（A4 引用，含智能体状态，供删除阻断与详情页展示）"""
    from backend.apps.agenthub.models import AgentSkill

    rows = (AgentSkill.objects.filter(skill=skill)
            .select_related('agent').order_by('agent__name'))
    return [{
        'agent_id': r.agent_id,
        'agent_name': r.agent.name,
        'agent_slug': r.agent.slug,
        'agent_status': r.agent.status,
        'agent_status_display': r.agent.get_status_display(),
        'published_version': r.agent.current_version,
        'mounted_at': r.added_at,
    } for r in rows]


def reference_count(skill: Skill) -> int:
    from backend.apps.agenthub.models import AgentSkill

    return AgentSkill.objects.filter(skill=skill).count()


# ─── C3 审核队列 ───

def review_queue():
    """待审核技能（管理员审核页默认视图）"""
    return Skill.objects.filter(status=SkillStatus.PENDING).order_by('created_at')


def status_summary() -> dict:
    """各状态计数，列表页顶部统计条用"""
    out = {key: 0 for key, _ in SkillStatus.choices}
    for status_value in Skill.objects.values_list('status', flat=True):
        out[status_value] = out.get(status_value, 0) + 1
    out['total'] = sum(v for k, v in out.items() if k != 'total')
    # 编辑稿待审核：这类技能的原启用版本仍在生效（C4）
    out['edited_pending'] = sum(
        1 for s in Skill.objects.filter(status=SkillStatus.PENDING) if s.approved_body)
    return out


def mountable_skills(user=None):
    """装配页可挂载集合（A4：只有已启用/内置技能可选，含有待审核编辑稿的在跑技能）

    C5：非管理员只能选择基础范围（部门）命中的技能；灰度比例不参与装配过滤
    （灰度是投放比例不是权限，装配人需能预先挂载），定向用户视为命中。
    """
    from django.db.models import Q

    from backend.apps.skills.models import SkillScope, SkillStatus as S
    qs = Skill.objects.filter(
        Q(status__in=(S.ACTIVE, S.BUILTIN))
        | Q(status=S.PENDING)).exclude(approved_body='')
    if user is None or user.is_staff or user.is_superuser:
        return qs
    from backend.apps.skills.permissions import visible_skill_query

    qs = qs.filter(visible_skill_query(user)).distinct()
    qs = qs.prefetch_related('applicable_users')
    scoped_ids = [s.pk for s in qs
                  if s.scope_mode == SkillScope.DEPARTMENTS
                  and not s.in_base_scope(user) and not s.is_targeted(user)]
    if scoped_ids:
        qs = qs.exclude(pk__in=scoped_ids)
    return qs
