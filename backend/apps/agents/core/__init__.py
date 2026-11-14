"""智能体内核：模型工厂 + 注册中心 + 事件桥接"""
from .factory import create_chat_model
from .registry import (
    AGENT_TYPE_CHAT,
    AGENT_TYPE_KNOWLEDGE,
    create_agent,
)
from .bridge import (
    create_agent_blocking, run_agent, run_agent_stream, run_agent_stream_async,
    run_profile_reply, run_profile_stream, run_profile_stream_async,
)

__all__ = [
    'create_chat_model',
    'AGENT_TYPE_CHAT',
    'AGENT_TYPE_KNOWLEDGE',
    'create_agent',
    'create_agent_blocking',
    'run_agent',
    'run_agent_stream',
    'run_agent_stream_async',
    'run_profile_reply',
    'run_profile_stream',
    'run_profile_stream_async',
]
