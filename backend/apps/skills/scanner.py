"""技能正文/附件风险扫描（PRD C2 / 9.2 #7 / 决策 10）

保存前对正文与附件做规则扫描，规则集中在本模块维护：

- 高危（high_risk）：命中后不直接阻断，由管理员显式确认（risk_ack）后放行
  ① 覆盖或绕过系统指令类的注入
  ② 读取凭据、密钥或环境变量
  ③ 静默外发数据
- 可疑（warnings）：只提示不阻断，随保存结果返回并留存在技能记录上

规则与规则集都带版本号，确认记录与审计可回溯"当时按哪一版规则放行"。
规则是启发式匹配，不是安全兜底；真正的审核仍由管理员在 C3 完成。
"""
import re
from typing import Dict, List

# 规则集版本：新增/调整规则时递增，旧的风险确认随规则集升级失效
RULESET_VERSION = '2026-11-07.1'

HIGH_RISK_RULES = [
    {
        'id': 'prompt_injection',
        'version': 1,
        'label': '覆盖或绕过系统指令',
        'reasons': [
            (r'(忽略|无视|抛开|抛掉)[^。\n]{0,8}(指令|要求|提示|限制|规则|约束)',
             '要求忽略既有指令'),
            (r'(绕过|跳过)(系统|安全|平台)?(指令|提示|限制|规则|审核)',
             '要求绕过系统约束'),
            (r'(覆盖|改写|替换)[^。\n]{0,6}(系统|平台|安全)[^。\n]{0,4}(指令|提示|设定)',
             '要求覆盖系统指令'),
            (r'\bignore\s+(all\s+)?(previous|above|prior)\s+instructions',
             '要求忽略既有指令（英文）'),
            (r'(system\s*prompt|系统提示词)', '提及系统提示词'),
            (r'(越狱|开发者模式|jailbreak)', '越狱/开发者模式'),
        ],
    },
    {
        'id': 'credential_access',
        'version': 1,
        'label': '读取凭据、密钥或环境变量',
        'reasons': [
            (r'(环境变量|process\.env|os\.environ)', '访问环境变量'),
            (r'(/etc/(passwd|shadow)|id_rsa|\.ssh/)', '访问系统凭据文件'),
            (r'(api[_-]?key|access[_-]?key|secret[_-]?key|credentials?\.json|\benv\b)',
             '提及密钥或凭据'),
            (r'(读取|获取|导出|遍历|打印)[^。\n]{0,8}(密码|口令|密钥|凭据)',
             '读取密码/密钥/凭据'),
        ],
    },
    {
        'id': 'silent_exfiltration',
        'version': 1,
        'label': '静默外发数据',
        'reasons': [
            (r'(curl|wget)\s+\S+', '使用命令行外发'),
            (r'(requests|urllib\.request)\s*\.?(post|put|urlopen)', '使用脚本外发请求'),
            (r'fetch\s*\(', '使用 fetch 外发请求'),
            (r'webhook', '推送至 webhook'),
            (r'(上传|发送|推送|回传)[^。\n]{0,6}(外部|第三方|远端|公网)', '向外部传输数据'),
        ],
    },
]

SUSPICIOUS_RULES = [
    {
        'id': 'destructive',
        'version': 1,
        'label': '破坏性文件操作',
        'reasons': [
            (r'(rm\s+-rf|mkfs|fdisk|format\s+[a-z]:)', '破坏性命令'),
            (r'(删除|清空)[^。\n]{0,6}(数据库|目录|全部文件)', '删除数据/文件'),
        ],
    },
    {
        'id': 'privilege',
        'version': 1,
        'label': '提权或系统级操作',
        'reasons': [
            (r'\bsudo\b', '使用 sudo'),
            (r'chmod\s+777', '放开文件权限'),
            (r'(管理员权限|系统权限|administrator)', '提权表述'),
        ],
    },
    {
        'id': 'network',
        'version': 1,
        'label': '外部网络访问',
        'reasons': [
            (r'https?://\S+', '包含外链'),
            (r'(调用|请求)[^。\n]{0,6}(外部|第三方)接口', '调用外部接口'),
        ],
    },
    {
        'id': 'command',
        'version': 1,
        'label': '命令执行',
        'reasons': [
            (r'(执行|运行)[^。\n]{0,6}(shell|命令|脚本)', '执行命令/脚本'),
            (r'(subprocess\.|os\.system)', '调用系统命令'),
        ],
    },
]


def _snippet(text: str, match: re.Match, width: int = 24) -> str:
    """命中片段兜底：去掉换行并截断，避免把整段正文回显给前端"""
    start = max(0, match.start() - 4)
    raw = ' '.join(text[start:start + width].split())
    return raw + ('…' if len(raw) >= width else '')


def _matches(text: str, rules: List[Dict], file: str = 'SKILL.md') -> List[Dict]:
    hits = []
    for rule in rules:
        for pattern, reason in rule['reasons']:
            found = re.search(pattern, text, re.IGNORECASE)
            if found:
                hits.append({
                    'rule': rule['id'],
                    'rule_version': rule.get('version', 1),
                    'ruleset_version': RULESET_VERSION,
                    'label': rule['label'],
                    'reason': reason,
                    'file': file,
                    'line': text.count('\n', 0, found.start()) + 1,
                    'snippet': _snippet(text, found),
                })
                break  # 同一规则同一文件只报一次，避免重复刷屏
    return hits


def scan_text(text: str, file: str = 'SKILL.md') -> Dict[str, List[Dict]]:
    """扫描单个文本，返回 {'high_risk': [...], 'warnings': [...]}"""
    content = text or ''
    return {
        'high_risk': _matches(content, HIGH_RISK_RULES, file),
        'warnings': _matches(content, SUSPICIOUS_RULES, file),
    }


def scan_body(body: str) -> Dict[str, List[Dict]]:
    """返回 {'high_risk': [...], 'blocked': 同 high_risk（兼容）, 'warnings': [...]}"""
    result = scan_text(body, 'SKILL.md')
    result['blocked'] = result['high_risk']
    return result


def describe(issue: Dict[str, str]) -> str:
    """单条命中转成前端可直接展示的中文说明"""
    return f'{issue["label"]}（片段：{issue["snippet"]}）'
