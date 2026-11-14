"""知识库信号：MCP 工具/来源停用时自动停用天工搜索绑定

绑定本身不保存凭据；工具被管理员关闭或标记失效后，下一次保存即把绑定置为停用，
避免搜索主路继续调用一个已停用工具。重新启用工具不会自动恢复绑定，需管理员确认。
"""
from django.db.models.signals import post_save
from django.dispatch import receiver

from backend.apps.knowledge.models import TiangongSearchBinding
from backend.apps.mcp.models import MCPServer, MCPTool


@receiver(post_save, sender=MCPTool)
def disable_binding_when_tool_unavailable(sender, instance, **kwargs):
    if instance.enabled and instance.state == 'present':
        return
    TiangongSearchBinding.objects.filter(tool=instance, enabled=True).update(enabled=False)


@receiver(post_save, sender=MCPServer)
def disable_binding_when_server_unavailable(sender, instance, **kwargs):
    if instance.is_active and instance.status not in ('disabled', 'egress_blocked'):
        return
    TiangongSearchBinding.objects.filter(server=instance, enabled=True).update(enabled=False)
