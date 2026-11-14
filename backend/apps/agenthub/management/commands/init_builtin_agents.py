"""注册出厂内置智能体（PRD 4.2）

内置智能体随平台版本自带，业务用户可直接使用、不可删除，管理员可在其上编辑，
改动过出厂配置就置「已自定义」并给出「恢复出厂」入口。

口径要点（与 PRD 4.2 一致）：
- 出厂即可用：建号后立即发布 v1，可见范围＝全企业，业务用户在对话页直接可选
- 不可删除：由 `AgentViewSet.destroy` 拦截内置件（只可归档隐藏）
- 不冲掉现场：库里与代码基线不一致时**只标记 is_customized，不自动覆盖**；
  要回基线必须显式 `--restore`（对应界面「恢复出厂」按钮）
- 恢复只改草稿，线上快照不变（7.3），需重新发布才影响用户侧
- model_name 由现场决定，不进出厂基线；建号时写入平台默认接入，避免每次对话出降级提示

用法：
    python manage.py init_builtin_agents             # 幂等注册/修复内置智能体
    python manage.py init_builtin_agents --restore   # 把内置智能体恢复到出厂基线
    python manage.py init_builtin_agents --check     # 只报告偏移，不落库
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from backend.apps.agenthub import builtin_agents
from backend.apps.agenthub.models import AgentProfile, AgentStatus
from backend.apps.common.llm import get_llm_config
from backend.apps.users.models import log_audit


class Command(BaseCommand):
    help = '注册/修复出厂内置智能体（PRD 4.2），幂等'

    def add_arguments(self, parser):
        parser.add_argument('--restore', action='store_true',
                            help='把内置智能体恢复到出厂基线（含移除工具来源与技能挂载）')
        parser.add_argument('--check', action='store_true', help='只报告与基线的偏移，不做任何写入')

    def handle(self, *args, **options):
        if options['check']:
            self._report_only()
            return

        for defn in builtin_agents.BUILTIN_AGENTS:
            agent = AgentProfile.objects.filter(name=defn['name']).first()
            if agent is None:
                self._register(defn)
            elif options['restore']:
                self._restore(agent, defn)
            else:
                self._repair(agent, defn)

    # ─── 建号 ───

    def _register(self, defn):
        name = defn['name']
        with transaction.atomic():
            agent = AgentProfile(
                name=name, description=defn['description'], job_tags=list(defn['job_tags']),
                icon=defn['icon'], speech_tier=defn['speech_tier'],
                retrieval_tier=defn['retrieval_tier'], enable_thinking=defn['enable_thinking'],
                prompt=defn['prompt'], knowledge_base_ids=list(defn['knowledge_base_ids']),
                builtin_capabilities=list(defn['builtin_capabilities']),
                permission_tier=defn['permission_tier'], visibility=defn['visibility'],
                model_name=get_llm_config('llm_model') or '',
                is_builtin=True, is_customized=False, status=AgentStatus.DRAFT,
            )
            agent.save()
            if agent.current_version == 0:
                agent.publish(None, '出厂内置注册')
        log_audit(actor=None, action='agent_create', target=f'智能体:{name}',
                  detail='出厂内置智能体注册（PRD 4.2），已发布为全企业可见')
        self.stdout.write(self.style.SUCCESS(f'已注册内置智能体：{name}（v{agent.current_version}）'))

    # ─── 已存在：只修复出厂标记，不覆盖现场配置 ───

    def _repair(self, agent, defn):
        changed = []
        if not agent.is_builtin:
            agent.is_builtin = True
            changed.append('is_builtin')
        drift = builtin_agents.drifted_fields(agent, defn)
        if (not drift) == bool(agent.is_customized):
            agent.is_customized = not drift
            changed.append('is_customized')
        if changed:
            agent.save(update_fields=changed + ['updated_at'])
        state = '出厂状态' if not drift else '已自定义：' + '、'.join(drift)
        self.stdout.write(self.style.SUCCESS(
            f'内置智能体已存在：{agent.name}（v{agent.current_version}，{state}，'
            f'修复字段={changed or "无"}）'))

    # ─── 显式恢复出厂 ───

    def _restore(self, agent, defn):
        drift = builtin_agents.drifted_fields(agent, defn)
        if not drift and not agent.is_customized:
            self.stdout.write(f'内置智能体仍是出厂状态，无需恢复：{agent.name}')
            return
        with transaction.atomic():
            written = builtin_agents.apply_factory(agent, defn)
            removed = builtin_agents.clear_builtin_mounts(agent)
            agent.is_builtin = True
            agent.is_customized = False
            fields = written + ['is_builtin', 'is_customized', 'updated_at']
            agent.save(update_fields=list(dict.fromkeys(fields)))
        log_audit(actor=None, action='agent_update', target=f'智能体:{agent.name}',
                  detail=f'恢复出厂配置：改写 {written or "无"}，移除挂载 {removed} 条；'
                         f'线上仍为 v{agent.current_version}，重新发布后新会话生效')
        self.stdout.write(self.style.SUCCESS(
            f'已恢复出厂：{agent.name}（改写 {len(written)} 项，移除挂载 {removed} 条）'))

    def _report_only(self):
        for defn in builtin_agents.BUILTIN_AGENTS:
            agent = AgentProfile.objects.filter(name=defn['name']).first()
            if agent is None:
                self.stdout.write(f'未注册：{defn["name"]}')
                continue
            drift = builtin_agents.drifted_fields(agent, defn)
            self.stdout.write(
                f'{agent.name}（v{agent.current_version}，状态={agent.get_status_display()}）：'
                + ('与出厂基线一致' if not drift else '偏移字段 ' + '、'.join(drift)))
