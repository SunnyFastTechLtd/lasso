import asyncio
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from backend.apps.agents.core.runtime import (
    MCPPlan, asl_tool_name, plan_mcp, plan_permission, plan_skills, render_prompt,
)
from backend.apps.agents.tools import build_skill_file_tools, search_knowledge_base
from backend.apps.skills.models import Skill, SkillScope, SkillStatus
from backend.apps.skills.services import sync_mirror
from backend.apps.users.models import Department, User


def _text_of(chunk):
    return ''.join(block.text for block in chunk.content)


class SkillScopeRuntimeTests(TestCase):
    """C5：技能生效范围（部门灰度）在运行时注入层生效"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        holder = override_settings(SKILLS_MIRROR_DIR=Path(self.tmp))
        holder.enable()
        self.addCleanup(holder.disable)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.parent = Department.objects.create(name='总部')
        self.child = Department.objects.create(name='研发中心', parent=self.parent)
        self.other = Department.objects.create(name='销售部')
        self.skill = Skill.objects.create(
            name='部门技能', slug='dept-skill', description='灰度',
            body='按部门生效的规范', approved_body='按部门生效的规范',
            status=SkillStatus.ACTIVE, scope_mode=SkillScope.DEPARTMENTS,
            scope_include_sub_departments=True)
        self.skill.applicable_departments.set([self.parent])
        sync_mirror(self.skill)

    def _user(self, name, department='', is_staff=False):
        return User.objects.create_user(username=name, password='x',
                                        department=department, is_staff=is_staff)

    def test_plan_skills_injects_for_matching_department(self):
        dirs, notes = plan_skills({'skills': ['dept-skill']},
                                  self._user('dev', department=self.child.name))
        self.assertEqual(dirs, [str(self.skill.mirror_dir)])
        self.assertEqual(notes, [])

    def test_plan_skills_degrades_for_other_department(self):
        dirs, notes = plan_skills({'skills': ['dept-skill']},
                                  self._user('sales', department=self.other.name))
        self.assertEqual(dirs, [])
        self.assertEqual(notes[0].kind, 'skill')
        self.assertIn('生效范围', notes[0].reason)

    def test_plan_skills_without_user_keeps_legacy_behavior(self):
        """后台/非交互调用不传 user 时不做范围过滤，避免误伤"""
        dirs, notes = plan_skills({'skills': ['dept-skill']}, None)
        self.assertEqual(dirs, [str(self.skill.mirror_dir)])

    def test_admin_always_in_scope(self):
        dirs, _ = plan_skills({'skills': ['dept-skill']},
                              self._user('boss', department=self.other.name, is_staff=True))
        self.assertEqual(dirs, [str(self.skill.mirror_dir)])

    def test_rollout_percent_blocks_user_outside_bucket(self):
        """灰度 0%：部门命中也不注入，降级原因说明是灰度比例未命中"""
        self.skill.scope_mode = SkillScope.ALL
        self.skill.rollout_percent = 0
        self.skill.save(update_fields=['scope_mode', 'rollout_percent'])
        user = self._user('gray-out', department=self.child.name)
        dirs, notes = plan_skills({'skills': ['dept-skill']}, user)
        self.assertEqual(dirs, [])
        self.assertIn('灰度', notes[0].reason)

    def test_targeted_user_bypasses_rollout(self):
        """定向用户白名单：0% 灰度下仍然注入"""
        self.skill.scope_mode = SkillScope.ALL
        self.skill.rollout_percent = 0
        self.skill.save(update_fields=['scope_mode', 'rollout_percent'])
        user = self._user('gray-in')
        self.skill.applicable_users.set([user])
        dirs, notes = plan_skills({'skills': ['dept-skill']}, user)
        self.assertEqual(dirs, [str(self.skill.mirror_dir)])
        self.assertEqual(notes, [])


class SkillFileToolTests(SimpleTestCase):
    """C1：read_skill_file 只读本次挂载目录内的文本附件"""

    def _tool(self, root: Path):
        tools = build_skill_file_tools({'demo': str(root)})
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].name, 'read_skill_file')
        return tools[0]

    def test_reads_attachment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'demo'
            (root / 'references').mkdir(parents=True)
            (root / 'references' / 'faq.md').write_text('常见问题', encoding='utf-8')
            chunk = asyncio.run(self._tool(root).call(skill='demo', path='references/faq.md'))
            self.assertEqual(chunk.state, 'success')
            self.assertIn('常见问题', _text_of(chunk))

    def test_rejects_traversal_and_unknown_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'demo'
            root.mkdir()
            (Path(tmp) / 'secret.txt').write_text('S', encoding='utf-8')
            tool = self._tool(root)
            traversal = asyncio.run(tool.call(skill='demo', path='../secret.txt'))
            self.assertEqual(traversal.state, 'error')
            unknown = asyncio.run(tool.call(skill='other', path='a.md'))
            self.assertEqual(unknown.state, 'error')
            missing = asyncio.run(tool.call(skill='demo', path='nope.md'))
            self.assertEqual(missing.state, 'error')


class SearchKnowledgeBaseToolTests(SimpleTestCase):
    """知识库检索工具：问题向量化 → 语义检索 → 格式化返回真实分块内容"""

    @patch('backend.apps.agents.tools.search_chunks')
    @patch('backend.apps.agents.tools.single_embedding')
    def test_hit_returns_chunk_contents_with_sources(self, mock_embed, mock_search):
        mock_embed.return_value = [1.0, 0.0]
        mock_search.return_value = [
            {
                'chunk_id': 1, 'chunk_index': 0, 'content': '机器学习是人工智能的分支',
                'score': 0.98, 'doc_title': 'AI 手册', 'file_name': 'ai.txt',
            },
            {
                'chunk_id': 2, 'chunk_index': 3, 'content': '深度学习使用多层神经网络',
                'score': 0.87, 'doc_title': 'AI 手册', 'file_name': 'ai.txt',
            },
        ]

        chunk = asyncio.run(search_knowledge_base('什么是机器学习', knowledge_base_id=2))

        self.assertEqual(chunk.state, 'success')
        mock_embed.assert_called_once_with('什么是机器学习')
        mock_search.assert_called_once_with(2, [1.0, 0.0], top_k=5)
        text = _text_of(chunk)
        self.assertIn('机器学习是人工智能的分支', text)
        self.assertIn('深度学习使用多层神经网络', text)
        self.assertIn('AI 手册', text)
        self.assertEqual(
            chunk.metadata['sources'],
            [{'doc_title': 'AI 手册', 'file_name': 'ai.txt', 'score': 0.98},
             {'doc_title': 'AI 手册', 'file_name': 'ai.txt', 'score': 0.87}],
        )

    @patch('backend.apps.agents.tools.search_chunks')
    @patch('backend.apps.agents.tools.single_embedding')
    def test_miss_prompts_insufficient_knowledge(self, mock_embed, mock_search):
        mock_embed.return_value = [0.0, 1.0]
        mock_search.return_value = []

        chunk = asyncio.run(search_knowledge_base('无关问题', knowledge_base_id=9))

        self.assertEqual(chunk.state, 'success')
        self.assertIn('知识库内容不足', _text_of(chunk))

    @patch('backend.apps.agents.tools.single_embedding')
    def test_embedding_failure_returns_error_chunk(self, mock_embed):
        mock_embed.side_effect = RuntimeError('embedding 服务不可用')

        chunk = asyncio.run(search_knowledge_base('问题', knowledge_base_id=1))

        self.assertEqual(chunk.state, 'error')
        self.assertIn('知识库检索失败', _text_of(chunk))


class AslToolNameTests(SimpleTestCase):
    """平台唯一键 → AgentScope 实际名（7.1 规范化口径）"""

    def test_ascii_passes_through(self):
        self.assertEqual(asl_tool_name('crm', 'query_order'), 'mcp__crm__query_order')

    def test_illegal_chars_replaced_with_x(self):
        # 中文、点、空格都不是 [a-zA-Z0-9_-]，逐字符替换为 x
        self.assertEqual(asl_tool_name('客服', '查.单'), 'mcp__xx__xxx')


class RenderPromptTests(SimpleTestCase):
    """A3 占位符替换与兜底"""

    def test_known_placeholders_filled(self):
        user = SimpleNamespace(username='demo.user', display_name='小赵', department='财务部')
        cfg = {'system_prompt': '{user_name}Today {current_date} in {department}'}
        out = render_prompt(cfg, user)
        self.assertIn('小赵', out)
        self.assertIn('财务部', out)
        self.assertNotIn('{user_name}', out)
        self.assertNotIn('{department}', out)

    def test_unknown_placeholder_unbraced(self):
        # 管理员手写未知占位符：去掉花括号原样保留，不猜语义
        cfg = {'system_prompt': '你好 {nickname}'}
        out = render_prompt(cfg, None)
        self.assertIn('nickname', out)
        self.assertNotIn('{nickname}', out)

    def test_empty_prompt_falls_back(self):
        cfg = {'system_prompt': '   '}
        out = render_prompt(cfg, None)
        self.assertIn('企业智能体助手', out)


def _plan(slug, raw_names):
    return MCPPlan(
        slug=slug, name=slug, transport='http', stateful=False,
        tool_names=list(raw_names),
        full_names=[f'mcp__{slug}__{n}' for n in raw_names],
        asl_names=[asl_tool_name(slug, n) for n in raw_names],
    )


class PlanPermissionTests(SimpleTestCase):
    """A5 档位 → 权限上下文映射"""

    def test_auto_maps_to_bypass(self):
        from agentscope.permission import PermissionMode
        plan = _plan('crm', ['query'])
        pp = plan_permission({'permission_tier': 'auto'}, [plan])
        self.assertEqual(pp.mode, PermissionMode.BYPASS)
        self.assertEqual(pp.allow_rules, {})

    def test_whitelist_allows_selected_denies_rest(self):
        from agentscope.permission import PermissionMode
        plan = _plan('crm', ['query', 'delete'])
        allow = 'mcp__crm__query'  # 只有 query 进白名单
        pp = plan_permission(
            {'permission_tier': 'whitelist', 'allow_tool_names': [allow]}, [plan])
        self.assertEqual(pp.mode, PermissionMode.DEFAULT)
        self.assertIn(allow, pp.allow_rules)
        self.assertIn('mcp__crm__delete', pp.deny_rules)

    def test_ask_allows_whitelist_and_asks_rest(self):
        from agentscope.permission import PermissionBehavior, PermissionMode
        plan = _plan('crm', ['query', 'delete'])
        allow = 'mcp__crm__query'
        pp = plan_permission(
            {'permission_tier': 'ask', 'allow_tool_names': [allow]}, [plan])
        self.assertEqual(pp.mode, PermissionMode.DEFAULT)
        self.assertIn(allow, pp.allow_rules)
        self.assertIn('mcp__crm__delete', pp.ask_rules)
        self.assertEqual(pp.deny_rules, {})
        rule = pp.ask_rules['mcp__crm__delete'][0]
        self.assertEqual(rule.behavior, PermissionBehavior.ASK)

    def test_ask_applies_session_grants_only_to_mounted(self):
        plan = _plan('crm', ['query', 'delete'])
        pp = plan_permission(
            {'permission_tier': 'ask', 'allow_tool_names': []}, [plan],
            session_grants=['mcp__crm__delete', 'mcp__gone__x'])
        self.assertIn('mcp__crm__delete', pp.allow_rules)
        self.assertNotIn('mcp__gone__x', pp.allow_rules)
        self.assertIn('mcp__crm__query', pp.ask_rules)

    def test_ask_non_interactive_falls_back_to_whitelist(self):
        plan = _plan('crm', ['query', 'delete'])
        allow = 'mcp__crm__query'
        pp = plan_permission(
            {'permission_tier': 'ask', 'allow_tool_names': [allow]}, [plan],
            interactive=False)
        self.assertIn(allow, pp.allow_rules)
        self.assertIn('mcp__crm__delete', pp.deny_rules)
        self.assertEqual(pp.ask_rules, {})

    def test_missing_tier_defaults_to_whitelist(self):
        from agentscope.permission import PermissionMode
        plan = _plan('crm', ['query'])
        pp = plan_permission({'allow_tool_names': []}, [plan])
        self.assertEqual(pp.mode, PermissionMode.DEFAULT)
        # 无白名单时勾选的工具也落进 deny（未进 allow）
        self.assertIn('mcp__crm__query', pp.deny_rules)


class _FakeTool:
    def __init__(self, name, enabled=True):
        self.name = name
        self.enabled = enabled


class _FakeToolsQS:
    def __init__(self, tools):
        self._tools = tools

    def filter(self, state=None):
        return self._tools


class _FakeServer:
    def __init__(self, slug, is_active=True, status='ready',
                 requires_egress=False, tools=None, transport='http_mcp'):
        self.slug = slug
        self.name = slug
        self.is_active = is_active
        self.status = status
        self.requires_egress = requires_egress
        self.transport = transport
        self.is_stateful = False
        self.url = ''
        self.headers = {}
        self.command = 'node'
        self.args = []
        self.cwd = ''
        self.effective_timeout = 30
        self._tools = tools or [_FakeTool('query')]

    def get_secrets(self):
        return {}

    @property
    def tools(self):
        return _FakeToolsQS(self._tools)


class PlanMcpDegradeTests(SimpleTestCase):
    """D4/D6：来源不可用与工具失效一律跳过并产出降级说明，绝不阻断"""

    def _cfg(self, server='crm', tools=('query',)):
        return {
            'mcp_servers': [{'server': server, 'tool_names': list(tools)}],
            'allow_tool_names': [],
        }

    def test_server_missing_degrades(self):
        with patch('backend.apps.mcp.models.MCPServer') as m, \
                patch('backend.apps.mcp.services.egress_allowed', return_value=True):
            m.objects.filter.return_value.first.return_value = None
            plans, notes = plan_mcp(self._cfg())
        self.assertEqual(plans, [])
        self.assertEqual(notes[0].kind, 'mcp')
        self.assertIn('不存在', notes[0].reason)

    def test_server_disabled_degrades(self):
        srv = _FakeServer('crm', is_active=False)
        with patch('backend.apps.mcp.models.MCPServer') as m, \
                patch('backend.apps.mcp.services.egress_allowed', return_value=True):
            m.objects.filter.return_value.first.return_value = srv
            plans, notes = plan_mcp(self._cfg())
        self.assertEqual(plans, [])
        self.assertIn('停用', notes[0].reason)

    def test_egress_blocked_degrades(self):
        srv = _FakeServer('crm', status='egress_blocked')
        with patch('backend.apps.mcp.models.MCPServer') as m, \
                patch('backend.apps.mcp.services.egress_allowed', return_value=True):
            m.objects.filter.return_value.first.return_value = srv
            plans, notes = plan_mcp(self._cfg())
        self.assertEqual(plans, [])
        self.assertIn('出网', notes[0].reason)

    def test_vanished_and_off_tools_degrade_but_usable_still_planned(self):
        # query 可用；gone 不在 present；off 在 present 但停用
        srv = _FakeServer('crm', tools=[
            _FakeTool('query'), _FakeTool('off', enabled=False)])
        with patch('backend.apps.mcp.models.MCPServer') as m, \
                patch('backend.apps.mcp.services.egress_allowed', return_value=True):
            m.objects.filter.return_value.first.return_value = srv
            plans, notes = plan_mcp(self._cfg(tools=('query', 'gone', 'off')))
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].tool_names, ['query'])
        reasons = ' '.join(n.reason for n in notes)
        self.assertIn('已不存在', reasons)   # gone
        self.assertIn('已关闭', reasons)      # off

    def test_no_usable_tools_skips_plan(self):
        srv = _FakeServer('crm', tools=[_FakeTool('off', enabled=False)])
        with patch('backend.apps.mcp.models.MCPServer') as m, \
                patch('backend.apps.mcp.services.egress_allowed', return_value=True):
            m.objects.filter.return_value.first.return_value = srv
            plans, notes = plan_mcp(self._cfg(tools=('off',)))
        self.assertEqual(plans, [])
        self.assertTrue(any('未注入' in n.reason for n in notes))

    def test_stdio_source_not_injected(self):
        # PRD #8 已决：命令型来源 P0 只支持注册与连通性测试，会话侧一律跳过并说明
        srv = _FakeServer('crm', transport='stdio')
        with patch('backend.apps.mcp.models.MCPServer') as m, \
                patch('backend.apps.mcp.services.egress_allowed', return_value=True):
            m.objects.filter.return_value.first.return_value = srv
            plans, notes = plan_mcp(self._cfg())
        self.assertEqual(plans, [])
        self.assertIn('会话调用未开放', notes[0].reason)


class _FakeStreamAgent:
    """只提供 reply_stream 的假 Agent，事件为真实 AgentScope 事件对象"""

    def __init__(self, events):
        self._events = events

    async def reply_stream(self, inputs):
        for ev in self._events:
            yield ev


def _a3_events(finish='exceed_max_iters', error=None, tools=('mcp__kb__search',)):
    from agentscope.event import (
        ReplyEndEvent, ReplyStartEvent, TextBlockDeltaEvent,
        ToolCallStartEvent,
    )

    rid = 'r1'
    events = [ReplyStartEvent(session_id='s1', reply_id=rid, name='test')]
    for i, name in enumerate(tools):
        events.append(ToolCallStartEvent(
            reply_id=rid, tool_call_id=f'c{i}', tool_call_name=name))
    events.append(TextBlockDeltaEvent(
        reply_id=rid, block_id='b1', delta='部分结果正文'))
    events.append(ReplyEndEvent(
        session_id='s1', reply_id=rid, finished_reason=finish, error=error))
    return events


class QueryLimitA3BridgeTests(SimpleTestCase):
    """A3：查询次数上限到顶必须显式提示（正文如实说明 + 过程区降级条）"""

    def _run(self, events, displays=None):
        from backend.apps.agents.core.bridge import _ToolTracker, run_agent_stream

        tracker = _ToolTracker(displays)
        agent = _FakeStreamAgent(events)
        return list(run_agent_stream(
            agent, [], '帮我查制度', tracker=tracker)), tracker

    def test_exceed_max_iters_appends_note_and_degrade(self):
        payloads, _ = self._run(
            _a3_events(), {'mcp__kb__search': '制度库·search'})
        chunks = [p for p in payloads if p['type'] == 'chunk']
        final = chunks[-1]['content']
        self.assertIn('已达本次查询次数上限', final)
        self.assertIn('没能查到完整结果', final)
        self.assertIn('已试过的来源：制度库·search', final)
        degrades = [p for p in payloads if p['type'] == 'degrade']
        self.assertEqual(len(degrades), 1)
        note = degrades[0]['notes'][0]
        self.assertEqual(note['name'], '本轮检索')
        self.assertIn('已达本次查询次数上限', note['reason'])

    def test_exceed_without_tools_states_no_query_done(self):
        payloads, _ = self._run(_a3_events(tools=()))
        final = [p for p in payloads if p['type'] == 'chunk'][-1]['content']
        self.assertIn('本轮未能完成任何外部查询', final)
        self.assertIn('已达本次查询次数上限', final)

    def test_normal_completion_stays_silent(self):
        payloads, _ = self._run(_a3_events(finish='completed'))
        self.assertFalse([p for p in payloads if p['type'] == 'degrade'])
        self.assertNotIn('查询次数上限',
                         ''.join(p.get('content', '') for p in payloads
                                 if p['type'] == 'chunk'))

    def test_error_takes_priority_over_limit_note(self):
        from agentscope.event._event import ErrorInfo

        payloads, _ = self._run(_a3_events(
            finish='error',
            error=ErrorInfo(message='模型调用失败')))
        self.assertEqual([p['type'] for p in payloads if
                          p['type'] in ('error', 'degrade')], ['error'])

    def test_iter_async_same_behavior(self):
        from backend.apps.agents.core.bridge import _ToolTracker, _iter_async

        tracker = _ToolTracker({'mcp__kb__search': '制度库·search'})
        agent = _FakeStreamAgent(_a3_events())

        async def _collect():
            return [p async for p in _iter_async(agent, [], tracker, None)]

        payloads = asyncio.run(_collect())
        self.assertTrue(any(p['type'] == 'degrade' for p in payloads))
        final = [p for p in payloads if p['type'] == 'chunk'][-1]['content']
        self.assertIn('已达本次查询次数上限', final)

    def test_profile_reply_accumulates_degrade_notes(self):
        """非流式路径：装配期降级条与触顶补发条都要保留（extend 而非替换）"""
        from backend.apps.agents.core import bridge

        stream = [
            {'type': 'degrade', 'notes': [{'name': '来源A', 'reason': '不可用'}]},
            {'type': 'chunk', 'content': '正文'},
            {'type': 'usage', 'input_tokens': 30, 'output_tokens': 8,
             'cache_input_tokens': 0, 'cache_creation_input_tokens': 0,
             'model_calls': 1},
            {'type': 'degrade', 'notes': [{'name': '本轮检索', 'reason': '已达本次查询次数上限'}]},
        ]
        with patch.object(bridge, 'run_profile_stream', return_value=iter(stream)):
            text, notes, usage = bridge.run_profile_reply(
                1, [], '问题', return_usage=True)
        self.assertEqual(text, '正文')
        self.assertEqual(len(notes), 2)
        self.assertEqual(notes[0]['name'], '来源A')
        self.assertEqual(usage['input_tokens'], 30)
        self.assertEqual(usage['output_tokens'], 8)
        self.assertEqual(usage['model_calls'], 1)


class ToolEgressFlagTests(SimpleTestCase):
    """F4：工具调用按装配期归属打标，出网与内网互斥"""

    def test_marks_only_egress_tools(self):
        from agentscope.event import ToolCallStartEvent, ToolResultEndEvent
        from agentscope.message import ToolResultState

        from backend.apps.agents.core.bridge import _ToolTracker

        tracker = _ToolTracker(
            displays={'mcp__kb__search': '制度库·search',
                      'mcp__cloud__web': '联网·web'},
            egress_tools={'mcp__cloud__web'},
            egress_hosts={'mcp__cloud__web': 'mcp.example.com'})

        cases = [('mcp__kb__search', False, ''), ('mcp__cloud__web', True,
                                                 'mcp.example.com')]
        for idx, (name, expected, host) in enumerate(cases):
            call_id = f'c{idx}'
            start = tracker.on(ToolCallStartEvent(
                reply_id='r1', tool_call_id=call_id, tool_call_name=name))
            self.assertEqual(start[0]['egress_host'], host)
            payloads = tracker.on(ToolResultEndEvent(
                reply_id='r1', tool_call_id=call_id,
                state=ToolResultState.SUCCESS))
            self.assertEqual(len(payloads), 1)
            self.assertEqual(payloads[0]['egress'], expected)
            self.assertEqual(payloads[0]['egress_host'], host)
            self.assertEqual(payloads[0]['name'], name)

    def test_default_is_internal(self):
        from agentscope.event import ToolCallStartEvent, ToolResultEndEvent
        from agentscope.message import ToolResultState

        from backend.apps.agents.core.bridge import _ToolTracker

        tracker = _ToolTracker()
        tracker.on(ToolCallStartEvent(
            reply_id='r1', tool_call_id='c1', tool_call_name='multi_kb_search'))
        payload = tracker.on(ToolResultEndEvent(
            reply_id='r1', tool_call_id='c1', state=ToolResultState.SUCCESS))[0]
        self.assertFalse(payload['egress'])


class ToolBodySideChannelTests(SimpleTestCase):
    """PRD 9.2 #4：全文走旁路，SSE payload 结构与 500 字摘要不变（防回归）"""

    PAYLOAD_KEYS = {'type', 'call_id', 'name', 'display', 'state', 'result',
                    'args', 'elapsed', 'egress', 'egress_host'}

    def test_payload_still_summary_and_full_kept_aside(self):
        from agentscope.event import (
            ToolCallStartEvent, ToolResultEndEvent, ToolResultTextDeltaEvent,
        )
        from agentscope.message import ToolResultState

        from backend.apps.agents.core.bridge import _ToolTracker

        raw = '第一行\n' + '数据' * 900 + '\n最后一行'
        tracker = _ToolTracker(displays={'mcp__kb__search': '制度库·search'})
        tracker.on(ToolCallStartEvent(
            reply_id='r1', tool_call_id='c1', tool_call_name='mcp__kb__search'))
        tracker.on(ToolResultTextDeltaEvent(
            reply_id='r1', tool_call_id='c1', delta=raw[:300]))
        tracker.on(ToolResultTextDeltaEvent(
            reply_id='r1', tool_call_id='c1', delta=raw[300:]))
        payload = tracker.on(ToolResultEndEvent(
            reply_id='r1', tool_call_id='c1', state=ToolResultState.SUCCESS))[0]

        # 结构不变：没有多出 result_full 之类的字段
        self.assertEqual(set(payload.keys()), self.PAYLOAD_KEYS)
        # 摘要仍被压平并截到 500（末尾省略号）
        self.assertLessEqual(len(payload['result']), 501)
        self.assertIn('…', payload['result'])
        self.assertNotIn('最后一行', payload['result'])
        # 全文可从旁路取走，且保留原始换行；取走后释放
        self.assertEqual(tracker.pop_full('c1'), raw)
        self.assertEqual(tracker.pop_full('c1'), '')


class ModelUsageTrackerTests(SimpleTestCase):
    """F4 真实 token 计量：模型调用结束事件的 usage 逐次累加"""

    def test_usage_events_accumulated(self):
        from agentscope.event import ModelCallEndEvent

        from backend.apps.agents.core.bridge import _ToolTracker

        tracker = _ToolTracker()
        first = tracker.on(ModelCallEndEvent(
            reply_id='r1', input_tokens=120, output_tokens=30,
            cache_input_tokens=10))[0]
        tracker.on(ModelCallEndEvent(
            reply_id='r1', input_tokens=80, output_tokens=20,
            cache_creation_input_tokens=5))

        self.assertEqual(first['type'], 'usage')
        self.assertEqual(first['input_tokens'], 120)
        self.assertEqual(tracker.usage, {
            'input_tokens': 200,
            'output_tokens': 50,
            'cache_input_tokens': 10,
            'cache_creation_input_tokens': 5,
            'model_calls': 2,
        })

