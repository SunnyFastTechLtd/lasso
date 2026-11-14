"""出厂内置智能体（PRD 4.2）

内置智能体随平台版本自带，口径三条：
- 业务用户可直接使用（出厂即发布，可见范围＝全企业）；
- 不可删除（`AgentViewSet.destroy` 拦截内置件，只可归档隐藏）；
- 管理员可在其上编辑，改动了出厂配置就置 `is_customized`，界面给「恢复出厂」。

出厂基线写在代码里（本模块），库里与基线不一致时**只标记、不自动覆盖**——
升级时不能冲掉现场调整，要回基线必须显式恢复（界面按钮或 `init_builtin_agents --restore`）。

「恢复出厂」只覆盖草稿配置，线上快照不变（7.3），需重新发布才影响用户侧。

model_name 是现场决定的（取决于客户配了哪个模型接入），因此不进出厂基线、
也不算作偏移；注册命令在建号时把平台默认接入写进去，避免每次对话都给降级提示。
"""
from __future__ import annotations

from typing import Dict, List, Optional

# 出厂基线。name 为企业内唯一键，注册与恢复都以它为准。
BUILTIN_AGENTS: List[Dict] = [
    {
        'name': '通用问答',
        'description': '不带工具与知识库的基线助手，回答通用问题，也是新建智能体的起点',
        'job_tags': [],
        'icon': 'Bot',
        'speech_tier': 'mid',
        'retrieval_tier': 'few',
        'enable_thinking': False,
        'prompt': (
            '你是企业的通用问答助手。直接回答问题，简洁清楚；'
            '不确定就明说不确定，不要编造事实、数据或来源。'
            '涉及公司内部制度、流程或系统数据时，提示用户改用挂了知识库或工具来源的智能体。'
            '当前日期：{current_date}，用户：{user_name}（{department}）。'
        ),
        'builtin_capabilities': [],
        'mcp_servers': [],
        'skills': [],
        'knowledge_base_ids': [],
        'permission_tier': 'whitelist',
        'visibility': 'public',
    },
    {
        'name': '企业知识助手',
        'description': '开启资料检索能力，先检索企业知识库再作答，并标注来源文档',
        'job_tags': ['知识管理'],
        'icon': 'BookOpen',
        'speech_tier': 'low',
        'retrieval_tier': 'standard',
        'enable_thinking': False,
        'prompt': (
            '你是企业的知识库助手。回答前先用 search_knowledge_base 检索企业知识库，'
            '依据检索到的内容作答，并在引用处注明来源文档名称；'
            '检索不到依据时明确说明「知识库中没有找到相关内容」，'
            '不要凭常识编造公司制度、流程或数据。'
            '当前日期：{current_date}，用户：{user_name}（{department}）。'
        ),
        'builtin_capabilities': ['knowledge_search'],
        'mcp_servers': [],
        'skills': [],
        'knowledge_base_ids': [],
        'permission_tier': 'whitelist',
        'visibility': 'public',
    },
]

# 参与出厂基线比对的字段（model_name 由现场决定，不比）
DRIFT_FIELDS = (
    'description', 'job_tags', 'icon', 'speech_tier', 'retrieval_tier',
    'enable_thinking', 'prompt', 'builtin_capabilities', 'knowledge_base_ids',
    'permission_tier', 'visibility',
)


def factory_of(name: str) -> Optional[Dict]:
    for defn in BUILTIN_AGENTS:
        if defn['name'] == name:
            return defn
    return None


def _current_of(agent, field: str):
    if field == 'job_tags':
        return list(agent.job_tags or [])
    if field == 'builtin_capabilities':
        return sorted(agent.builtin_capabilities or [])
    if field == 'knowledge_base_ids':
        return sorted(int(i) for i in (agent.knowledge_base_ids or []))
    return getattr(agent, field)


def _factory_of(defn: Dict, field: str):
    if field == 'builtin_capabilities':
        return sorted(defn.get(field) or [])
    if field == 'knowledge_base_ids':
        return sorted(int(i) for i in (defn.get(field) or []))
    return defn.get(field)


def mount_drift(agent, defn: Dict) -> bool:
    """内置件出厂不挂任何来源与技能；挂了就算被定制过"""
    if (defn.get('mcp_servers') or defn.get('skills')):
        return True
    return agent.agentmcpserver_set.exists() or agent.agentskill_set.exists()


def drifted_fields(agent, defn: Dict) -> List[str]:
    """当前配置相对出厂基线偏移了哪些字段（空表示仍是出厂状态）"""
    changed = [f for f in DRIFT_FIELDS if _current_of(agent, f) != _factory_of(defn, f)]
    if mount_drift(agent, defn):
        changed.append('mounts')
    return changed


def apply_factory(agent, defn: Dict) -> List[str]:
    """把出厂基线写回实例（不落库），返回实际被改写的字段"""
    changed: List[str] = []
    for field in DRIFT_FIELDS:
        target, current = _factory_of(defn, field), _current_of(agent, field)
        if current == target:
            continue
        setattr(agent, field, list(target) if isinstance(target, list) else target)
        changed.append(field)
    return changed


def clear_builtin_mounts(agent) -> int:
    """恢复出厂时移除内置件的工具来源与技能挂载，返回移除条数"""
    from backend.apps.agenthub.models import AgentMCPServer, AgentSkill

    removed = agent.agentmcpserver_set.count() + agent.agentskill_set.count()
    AgentMCPServer.objects.filter(agent=agent).delete()
    AgentSkill.objects.filter(agent=agent).delete()
    return removed
