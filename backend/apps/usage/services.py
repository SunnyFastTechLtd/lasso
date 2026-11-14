"""F4 用量概览（P0 只读切片 + P1 三维度完整版）

口径写死五项：会话数 / 提问人数 / 内网工具调用次数 / 出网调用次数 /
失败或被拒次数。工具调用按装配期归属互斥计入内网或出网，同一调用不重复计数。
完整版在同一批留痕上再按「智能体 / 工具来源 / 部门」三个维度聚合调用次数、
失败率与平均耗时，并提供导出；仍然不提供会话正文、提问内容、回答内容与
按个人的明细下钻，任何返回正文的写法都属于越界。

可见范围：管理员看全企业；其他人只能看自己所属部门（含同名部门字符串），
"部门主管"在 P0 不是一个独立角色，而是"业务用户 + 这张只读页"。
"""
from collections import defaultdict
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from django.db.models import Avg, Count, Q, Sum
from django.db.utils import DatabaseError
from django.db.models.functions import TruncDay, TruncWeek

from backend.apps.chat.models import Message
from backend.apps.users.models import AuditLog

GRANULARITY_TRUNC = {'day': TruncDay, 'week': TruncWeek}

# 悬浮说明：让主管把"失败次数"读成"某次查询没成功"，而不是"系统坏了"
DEFINITIONS = {
    'conversations': '区间内产生过提问的会话去重数；同一会话跨天提问，各天各计一次',
    'askers': '区间内发起过提问的人数去重；只看不提问的人不计入',
    'tool_calls': '智能体对「内网来源」工具发起的调用次数，成功与失败都算（已发起即计数），'
                  '不含走外网出口的调用',
    'egress_calls': '智能体对「出网来源」工具发起的调用次数，与内网工具调用互斥、不重复计入；'
                    '出网总开关关闭时恒为 0',
    'failures': '工具调用返回失败、被打断或被授权档位拒绝的次数（含内网与出网），'
                '通常是某次没查到，不代表系统故障',
    'input_tokens': '助手回答实际消耗的输入 token 数，取自模型服务返回的 usage；'
                    '仅统计成功落库的助手消息',
    'output_tokens': '助手回答实际生成的输出 token 数，取自模型服务返回的 usage；'
                     '仅统计成功落库的助手消息',
}

# F4 完整版：三个固定统计维度，顺序即界面默认顺序
BREAKDOWN_DIMENSIONS = ('agent', 'source', 'department')

BREAKDOWN_KEYS = {
    'agent': 'agent_name',
    'source': 'source_name',
    'department': 'actor__department',
}

BREAKDOWN_LABELS = {'agent': '按智能体', 'source': '按工具来源', 'department': '按部门'}

BREAKDOWN_DEFINITIONS = {
    'agent': '按工具调用发生时的智能体名称快照聚合；智能体改名或删除后，历史调用仍归到原名称',
    'source': '按工具调用发生时的来源名称快照聚合；失败率＝失败/被拒次数 ÷ 调用次数，'
              '平均耗时只统计有耗时记录的调用',
    'department': '按提问人当前归属部门聚合；未归口部门或账号已清理的调用记为「未记录」',
}

# 展示用占位：升级前未落快照列的历史留痕归入此组
UNRECORDED_LABEL = '未记录'

# 单维度最多返回多少行，避免一个来源过多时把响应撑大（按调用次数取前 N）
BREAKDOWN_LIMIT = 50

METRIC_KEYS = ('conversations', 'askers', 'tool_calls', 'egress_calls',
               'failures', 'input_tokens', 'output_tokens')

# 最长的一个统计区间，超过就拒绝——一体机不做全表扫描
MAX_SPAN_DAYS = 366


def _bucket_label(value) -> str:
    """把 Trunc 的结果统一成 YYYY-MM-DD 字符串（按周时为该周周一）"""
    return value.date().isoformat() if hasattr(value, 'date') else str(value)[:10]


def _all_buckets(start: date, end: date, granularity: str) -> List[str]:
    """补齐区间内的每个刻度，缺数据的日子也要出现在折线上"""
    step = 1 if granularity == 'day' else 7
    labels, cursor = [], start
    if granularity == 'week':
        cursor = start - timedelta(days=start.weekday())  # 回退到周一
    while cursor <= end:
        labels.append(cursor.isoformat())
        cursor += timedelta(days=step)
    return labels


def resolve_scope(user, scope: str, department: str):
    """把请求参数换成可信的部门过滤条件，返回 (scope, department, 拒绝理由)

    非管理员想看全企业、或改参数看别的部门，一律 403 而不是静默改写：
    静默改写会让界面写着"全企业"而数字其实是本部门，这比报错更糟。
    """
    is_staff = bool(user.is_staff or user.is_superuser)
    own = (getattr(user, 'department', '') or '').strip()

    if scope != 'dept':
        if not is_staff:
            return 'all', None, '只有平台管理员可以看全企业用量'
        return 'all', None, ''

    # 部门口径：管理员可指定任意部门，其他人只能是自己部门
    chosen = (department or '').strip() or own
    if not chosen:
        return 'dept', None, '你还没有归口部门，暂时看不到本部门用量'
    if not is_staff and chosen != own:
        return 'dept', chosen, '只能查看本部门的用量'
    return 'dept', chosen, ''


def _message_qs(start: date, end: date, department: Optional[str]):
    qs = Message.objects.filter(
        role='user', created_at__date__range=(start, end))
    if department:
        qs = qs.filter(conversation__user__department=department)
    return qs


def _answer_qs(start: date, end: date, department: Optional[str]):
    """助手消息：真实 token 计量的唯一数据源"""
    qs = Message.objects.filter(
        role='assistant', created_at__date__range=(start, end))
    if department:
        qs = qs.filter(conversation__user__department=department)
    return qs


def _audit_qs(start: date, end: date, department: Optional[str], actions: Tuple[str, ...]):
    qs = AuditLog.objects.filter(
        action__in=actions, created_at__date__range=(start, end))
    if department:
        qs = qs.filter(actor__department=department)
    return qs


def _tool_audit_qs(start: date, end: date, department: Optional[str]):
    """工具调用留痕（成功 + 失败/被拒），三维度统计的唯一数据源"""
    return _audit_qs(start, end, department, ('tool_call', 'tool_failed'))


def _breakdown_rows(qs, dimension: str) -> List[dict]:
    """把工具调用留痕按单一维度聚合：调用次数 / 失败率 / 平均耗时"""
    key = BREAKDOWN_KEYS[dimension]
    rows = (qs.values(key)
              .annotate(
                  calls=Count('id'),
                  failures=Count('id', filter=Q(action='tool_failed')),
                  avg_elapsed_ms=Avg('elapsed_ms'),
              )
              .order_by('-calls', key))
    if dimension != 'department':
        rows = rows[:BREAKDOWN_LIMIT]

    result = []
    for row in rows:
        calls = row['calls'] or 0
        failures = row['failures'] or 0
        avg = row['avg_elapsed_ms']
        result.append({
            'key': (row[key] or '').strip() or UNRECORDED_LABEL,
            'calls': calls,
            'failures': failures,
            'failure_rate': round(failures / calls, 4) if calls else 0.0,
            'avg_elapsed_ms': int(round(avg)) if avg is not None else None,
        })
    return result


def build_breakdowns(*, start: date, end: date, department: Optional[str]) -> Dict[str, List[dict]]:
    """F4 完整版：智能体 / 工具来源 / 部门三个维度的只读聚合"""
    qs = _tool_audit_qs(start, end, department)
    return {dim: _breakdown_rows(qs, dim) for dim in BREAKDOWN_DIMENSIONS}


def _merge(buckets: Dict[str, Dict[str, int]], rows, key: str) -> None:
    """把一条 GROUP BY 结果写进对应刻度，n 为该指标的计数别名"""
    for row in rows:
        buckets[_bucket_label(row['bucket'])][key] = row['n']


def build_overview(*, granularity: str, start: date, end: date,
                   scope: str, department: Optional[str]) -> dict:
    """产出 F4 的响应体（与前端 UsageOverview 逐字段对齐）"""
    trunc = GRANULARITY_TRUNC[granularity]
    buckets: Dict[str, Dict[str, int]] = defaultdict(dict)

    msg_qs = _message_qs(start, end, department)
    _merge(buckets, msg_qs.annotate(bucket=trunc('created_at')).values('bucket').annotate(
        n=Count('conversation_id', distinct=True)), 'conversations')
    _merge(buckets, msg_qs.annotate(bucket=trunc('created_at')).values('bucket').annotate(
        n=Count('conversation__user_id', distinct=True)), 'askers')
    _merge(buckets, _audit_qs(start, end, department, ('tool_call', 'tool_failed'))
           .filter(egress=False)
           .annotate(bucket=trunc('created_at')).values('bucket').annotate(n=Count('id')),
           'tool_calls')
    _merge(buckets, _audit_qs(start, end, department, ('tool_call', 'tool_failed'))
           .filter(egress=True)
           .annotate(bucket=trunc('created_at')).values('bucket').annotate(n=Count('id')),
           'egress_calls')
    _merge(buckets, _audit_qs(start, end, department, ('tool_failed',))
           .annotate(bucket=trunc('created_at')).values('bucket').annotate(n=Count('id')),
           'failures')
    answer_qs = _answer_qs(start, end, department)
    _merge(buckets, answer_qs.annotate(bucket=trunc('created_at')).values('bucket')
           .annotate(n=Sum('input_tokens')), 'input_tokens')
    _merge(buckets, answer_qs.annotate(bucket=trunc('created_at')).values('bucket')
           .annotate(n=Sum('output_tokens')), 'output_tokens')

    series = [{'bucket': label,
               **{k: buckets.get(label, {}).get(k, 0) for k in METRIC_KEYS}}
              for label in _all_buckets(start, end, granularity)]

    # 总计不是各刻度相加：会话数与提问人数都要在整段区间上重新去重，
    # 否则一个人周一周二各问一次会被算成两个人
    totals = {
        'conversations': msg_qs.values('conversation_id').distinct().count(),
        'askers': msg_qs.values('conversation__user_id').distinct().count(),
        'tool_calls': _audit_qs(start, end, department,
                                ('tool_call', 'tool_failed')).filter(egress=False).count(),
        'egress_calls': _audit_qs(start, end, department,
                                  ('tool_call', 'tool_failed')).filter(egress=True).count(),
        'failures': _audit_qs(start, end, department, ('tool_failed',)).count(),
        'input_tokens': answer_qs.aggregate(total=Sum('input_tokens'))['total'] or 0,
        'output_tokens': answer_qs.aggregate(total=Sum('output_tokens'))['total'] or 0,
    }

    return {
        'scope': scope,
        'department': department,
        'granularity': granularity,
        'start': start.isoformat(),
        'end': end.isoformat(),
        'metrics': totals,
        'series': series,
        'definitions': dict(DEFINITIONS),
        'breakdowns': build_breakdowns(start=start, end=end, department=department),
        'breakdown_definitions': dict(BREAKDOWN_DEFINITIONS),
    }


EXPORT_HEADER = ('维度', '名称', '调用次数', '失败或被拒次数', '失败率', '平均耗时(ms)')


def build_export_rows(*, start: date, end: date, department: Optional[str]) -> List[List]:
    """F4 完整版导出：区间汇总 + 三个维度的明细行（不含任何正文与个人明细）

    失败率写成 0.0000 形式的比例值（与接口字段 failure_rate 同口径），
    平均耗时只统计有耗时记录的调用；缺失时留空而不是写 0。
    """
    qs = _tool_audit_qs(start, end, department)
    calls = qs.count()
    failures = qs.filter(action='tool_failed').count()
    avg = qs.aggregate(value=Avg('elapsed_ms'))['value']
    rows: List[List] = [[
        '汇总',
        f'{start.isoformat()} ~ {end.isoformat()}',
        calls,
        failures,
        f'{(failures / calls) if calls else 0:.4f}',
        '' if avg is None else int(round(avg)),
    ]]

    breakdowns = build_breakdowns(start=start, end=end, department=department)
    for dim in BREAKDOWN_DIMENSIONS:
        for item in breakdowns[dim]:
            rows.append([
                BREAKDOWN_LABELS[dim],
                item['key'],
                item['calls'],
                item['failures'],
                f'{item["failure_rate"]:.4f}',
                '' if item['avg_elapsed_ms'] is None else item['avg_elapsed_ms'],
            ])
    return rows


def safe_export_rows(**kwargs) -> List[List]:
    """数据库异常时导出只有表头的空表，不让下载入口 500"""
    try:
        return build_export_rows(**kwargs)
    except DatabaseError:
        return []


def safe_overview(**kwargs) -> dict:
    """数据库异常时返回空看板而不是 500

    用量页是只读附属页，不该因为一次聚合失败把整个后台打挂。
    """
    try:
        return build_overview(**kwargs)
    except DatabaseError:
        return {
            'scope': kwargs['scope'],
            'department': kwargs['department'],
            'granularity': kwargs['granularity'],
            'start': kwargs['start'].isoformat(),
            'end': kwargs['end'].isoformat(),
            'metrics': {k: 0 for k in METRIC_KEYS},
            'series': [],
            'definitions': dict(DEFINITIONS),
            'breakdowns': {dim: [] for dim in BREAKDOWN_DIMENSIONS},
            'breakdown_definitions': dict(BREAKDOWN_DEFINITIONS),
            'error': '用量统计暂时不可用，请稍后重试',
        }
