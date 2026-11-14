"""女娲的开关与限额（全部落 SystemSetting，默认值在代码里）

三个开关语义完全分开（设计文档 5.1）：
- nuwa_market_online   女娲市场检索是否出网（默认开）
- nuwa_knowledge_online 女娲资料抓取是否出网（默认开）
- mcp_egress_enabled    智能体运行时工具是否出网（既有，默认关，本模块不碰）
"""
from backend.apps.settings.models import SystemSetting

KEY_MARKET_ONLINE = 'nuwa_market_online'
KEY_KNOWLEDGE_ONLINE = 'nuwa_knowledge_online'
KEY_ALLOWLIST = 'nuwa_knowledge_allowlist'
KEY_DAILY_LIMIT = 'nuwa_daily_session_limit'
KEY_ENABLED = 'nuwa_enabled'

DEFAULTS = {
    KEY_MARKET_ONLINE: 'true',
    KEY_KNOWLEDGE_ONLINE: 'true',
    KEY_ENABLED: 'true',
    KEY_DAILY_LIMIT: '20',
}


def _flag(key: str) -> bool:
    raw = SystemSetting.get_value(key, DEFAULTS.get(key, 'false'))
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


def nuwa_enabled() -> bool:
    return _flag(KEY_ENABLED)


def market_online() -> bool:
    return _flag(KEY_MARKET_ONLINE)


def knowledge_online() -> bool:
    return _flag(KEY_KNOWLEDGE_ONLINE)


def daily_limit() -> int:
    try:
        return max(1, int(SystemSetting.get_value(KEY_DAILY_LIMIT,
                                                  DEFAULTS[KEY_DAILY_LIMIT])))
    except (TypeError, ValueError):
        return 20


def knowledge_allowlist() -> list:
    import json

    raw = SystemSetting.get_value(KEY_ALLOWLIST, '')
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return [str(item).strip().lower() for item in data if str(item).strip()]


def set_flags(*, market_online_value=None, knowledge_online_value=None,
              allowlist=None, actor=None) -> dict:
    """管理员切换开关；只写传了的项，每次变更入审计"""
    from backend.apps.users.models import log_audit

    changed = []
    if market_online_value is not None:
        SystemSetting.set_value(KEY_MARKET_ONLINE, 'true' if market_online_value else 'false',
                                '女娲市场检索是否允许出网')
        changed.append(f'市场检索={"开" if market_online_value else "关"}')
    if knowledge_online_value is not None:
        SystemSetting.set_value(KEY_KNOWLEDGE_ONLINE,
                                'true' if knowledge_online_value else 'false',
                                '女娲资料抓取是否允许出网')
        changed.append(f'资料抓取={"开" if knowledge_online_value else "关"}')
    if allowlist is not None:
        import json

        SystemSetting.set_value(KEY_ALLOWLIST,
                                json.dumps([str(d).strip().lower().rstrip('.')
                                            for d in allowlist if str(d).strip()],
                                           ensure_ascii=False),
                                '女娲资料抓取的域名白名单（空=不限域名）')
        changed.append(f'域名白名单={len(allowlist)} 条')
    if changed:
        log_audit(actor=actor, action='nuwa_switches_update', target='女娲出网开关',
                  detail='；'.join(changed))
    return switches()


def switches() -> dict:
    from backend.apps.mcp.services import egress_allowed

    return {
        'enabled': nuwa_enabled(),
        'market_online': market_online(),
        'knowledge_online': knowledge_online(),
        'knowledge_allowlist': knowledge_allowlist(),
        'daily_limit': daily_limit(),
        # 只读展示，用于界面说明"与女娲无关"
        'agent_tool_egress': egress_allowed(),
    }
