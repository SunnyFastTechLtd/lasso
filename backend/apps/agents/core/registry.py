"""
智能体注册中心

Lasso 只保留两类运行时入口：通用对话（chat）与知识库问答（knowledge）。
办公类模块（写作/表格/导图/翻译/PPT/私人助理/阅读）已整体下线，
能力扩展改由「智能体定义（AgentProfile）+ MCP 工具来源 + 技能」在界面上装配，
装配逻辑接入点即本文件的 create_agent。

设计要点：
- 智能体不缓存、每次请求新建，保证设置页的模型配置变更即时生效
- 历史消息由调用方从数据库取出后通过 reply_stream 注入，
  Agent 本身保持无状态，天然支持多用户并发
- Toolkit.add_tool 是异步方法，因此 create_agent 定义为协程，
  由桥接层在事件循环中调用
"""
from typing import Callable, List, Optional

from agentscope.agent import Agent
from agentscope.tool import Toolkit, FunctionTool
from asgiref.sync import sync_to_async

from .factory import create_chat_model
from ..tools import (
    build_chat_tools, build_knowledge_tools, build_multi_knowledge_tools,
)

# 智能体类型标识（与会话 Conversation.type 对齐）
AGENT_TYPE_CHAT = 'chat'
AGENT_TYPE_KNOWLEDGE = 'knowledge'


# ─── System Prompts ───

CHAT_SYSTEM_PROMPT = (
    '你是 Lasso 企业级智能体平台的智能助手，由企业私有部署的大模型驱动。'
    '请用简洁、专业、友好的中文回答用户的问题。'
    '当用户明确要求生成 Word(docx)、Excel(xlsx)、PPT(pptx) 或 PDF 文件时，'
    '请调用 generate_file 工具生成真实可下载文件，并在最终回复中告知用户文件已生成。'
)

# 对话中勾选知识库时追加到通用助手 system prompt 的检索要求
CHAT_KB_PROMPT_SUFFIX = (
    '当前对话已关联知识库，回答资料性问题时必须先调用 search_knowledge_base 工具'
    '检索所选知识库，并严格基于检索到的资料作答、注明来源文档；'
    '检索结果中没有相关信息时，明确告知用户该问题暂无相关资料，不要编造。'
)

KNOWLEDGE_SYSTEM_PROMPT = (
    '你是 Lasso 平台的知识库问答助手。'
    '回答用户问题前，必须先调用 search_knowledge_base 工具检索知识库内容，'
    '并严格基于检索到的资料回答，不要编造；'
    '如果检索结果中没有相关信息，明确告知用户"该问题暂无相关资料"；'
    '回答要条理清晰，引用资料时注明来源文档标题。'
)


class AgentSpec:
    """智能体规格：名称 + system prompt + 工具构建器（可选）"""

    def __init__(
        self,
        name: str,
        system_prompt: str,
        tools_builder: Optional[Callable[[Optional[int], Optional[int]], List[FunctionTool]]] = None,
    ):
        self.name = name
        self.system_prompt = system_prompt
        self.tools_builder = tools_builder


_REGISTRY = {
    AGENT_TYPE_CHAT: AgentSpec(
        name='通用助手',
        system_prompt=CHAT_SYSTEM_PROMPT,
        tools_builder=build_chat_tools,
    ),
    AGENT_TYPE_KNOWLEDGE: AgentSpec(
        name='知识库助手',
        system_prompt=KNOWLEDGE_SYSTEM_PROMPT,
        tools_builder=build_knowledge_tools,
    ),
}


def get_spec(agent_type: str) -> AgentSpec:
    """获取智能体规格，未注册的类型回退为通用助手"""
    return _REGISTRY.get(agent_type, _REGISTRY[AGENT_TYPE_CHAT])


async def create_agent(
    agent_type: str = AGENT_TYPE_CHAT,
    resource_id: Optional[int] = None,
    stream: bool = True,
    temperature: Optional[float] = None,
    user_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    knowledge_base_ids: Optional[List[int]] = None,
) -> Agent:
    """
    按需创建智能体实例

    Args:
        agent_type: 智能体类型（chat / knowledge）
        resource_id: 关联资源 ID（知识库 ID），带工具的智能体必须提供
        stream: 是否启用模型流式输出
        temperature: 请求级温度，默认使用系统配置
        user_id: 当前用户 ID（chat 智能体生成文件时使用）
        conversation_id: 当前会话 ID（chat 智能体生成文件时使用）
        knowledge_base_ids: 对话中勾选的知识库 ID 列表（仅 chat 类型生效，
            为通用助手追加跨库检索工具）

    Returns:
        装配完成的 AgentScope Agent 实例
    """
    spec = get_spec(agent_type)
    # create_chat_model 内部会同步查询数据库配置（get_llm_config），
    # 本协程运行在事件循环的 async 上下文中，Django 禁止其中直接做
    # 同步 ORM 调用（SynchronousOnlyOperation），须切到线程池执行
    model = await sync_to_async(create_chat_model)(stream=stream, temperature=temperature)

    kb_ids = list(knowledge_base_ids or [])
    tools: List[FunctionTool] = []

    # chat 工具依赖 user_id / conversation_id；知识库工具依赖 resource_id
    if agent_type == AGENT_TYPE_CHAT:
        if user_id is not None:
            tools.extend(build_chat_tools(user_id, resource_id, conversation_id))
        if kb_ids:
            tools.extend(build_multi_knowledge_tools(kb_ids, user_id))
    elif spec.tools_builder is not None and resource_id is not None:
        tools.extend(spec.tools_builder(resource_id, user_id))

    toolkit = None
    if tools:
        toolkit = Toolkit()
        for tool in tools:
            await toolkit.add_tool(tool)

    system_prompt = spec.system_prompt
    if agent_type == AGENT_TYPE_CHAT and kb_ids:
        system_prompt = f'{system_prompt}{CHAT_KB_PROMPT_SUFFIX}'

    return Agent(
        name=spec.name,
        system_prompt=system_prompt,
        model=model,
        toolkit=toolkit,
    )
