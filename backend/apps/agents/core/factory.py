"""
AgentScope 模型工厂

将数据库「系统设置 → 模型接入与凭据」里的模型配置桥接为 AgentScope 的
模型对象，保证设置页的配置变更即时生效。环境变量不参与模型配置。
"""
from typing import Optional

from agentscope.credential import OpenAICredential
from agentscope.model import OpenAIChatModel

from backend.apps.common.llm import load_model_config


def create_chat_model(
    stream: bool = True,
    model_name: Optional[str] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    enable_thinking: bool = False,
) -> OpenAIChatModel:
    """
    基于当前数据库配置创建 AgentScope 聊天模型

    每次调用都读取最新配置（load_model_config 带缓存，
    配置更新时 reset_client 会清缓存），不缓存模型实例，
    保证设置页修改后新请求立即生效；配置不完整时抛
    ModelConfigError，提示去平台模型配置界面补齐。

    Args:
        stream: 是否启用流式输出
        model_name: 指定模型名，默认使用配置中的主模型
        temperature: 请求级温度，默认使用配置值
        top_p: 说话方式档位换算的核采样，None 时不下发该参数
        enable_thinking: A3「思考过程」开关，映射到 OpenAI 兼容端点的
            thinking_enable（非推理模型会被服务端忽略）
    """
    cfg = load_model_config()
    model = model_name or cfg['model']
    if temperature is None:
        temperature = cfg['temperature']

    credential = OpenAICredential(
        api_key=cfg['api_key'],
        base_url=cfg['base_url'],
    )

    parameters = OpenAIChatModel.Parameters(
        temperature=temperature,
        max_tokens=cfg['max_tokens'],
        top_p=top_p,
        thinking_enable=bool(enable_thinking),
    )

    return OpenAIChatModel(
        credential=credential,
        model=model,
        parameters=parameters,
        stream=stream,
        max_retries=2,
        retry_delay=1.0,
        client_kwargs={'timeout': cfg['timeout']},
    )
