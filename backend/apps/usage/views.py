"""F4 用量概览接口：只读、只聚合、不下钻；完整版另提供三维度 CSV 导出"""
import csv
import io
from datetime import date, timedelta

from django.http import HttpResponse
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from backend.apps.usage.services import (
    EXPORT_HEADER,
    MAX_SPAN_DAYS,
    resolve_scope,
    safe_export_rows,
    safe_overview,
)

DEFAULT_DAYS = 30


def _parse_date(value, field):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValidationError({field: '日期需用 YYYY-MM-DD 格式'})


def _validated_range(request):
    """解析并校验 start/end：默认近 30 天，最长 366 天"""
    today = date.today()
    end = _parse_date(request.query_params.get('end') or today.isoformat(), 'end')
    start = _parse_date(
        request.query_params.get('start') or (end - timedelta(days=DEFAULT_DAYS - 1)).isoformat(),
        'start',
    )
    if start > end:
        raise ValidationError({'start': '开始日期不能晚于结束日期'})
    if (end - start).days + 1 > MAX_SPAN_DAYS:
        raise ValidationError({'end': f'统计区间最长 {MAX_SPAN_DAYS} 天'})
    return start, end


def _resolved_scope(request):
    scope, department, denied = resolve_scope(
        request.user,
        request.query_params.get('scope', 'all'),
        request.query_params.get('department', ''),
    )
    if denied:
        raise PermissionDenied(denied)
    return scope, department


class UsageOverviewView(APIView):
    """GET /api/usage/overview/

    参数：granularity=day|week、start、end、scope=all|dept、department（仅管理员可用）。
    不给分页、不给排序、不给字段筛选——这些都是下钻的入口，P0 明确排除。
    响应在五项计数之外附带 breakdowns（智能体/来源/部门），供完整版看板展示。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        granularity = request.query_params.get('granularity', 'day')
        if granularity not in ('day', 'week'):
            raise ValidationError({'granularity': '只支持 day 或 week'})

        start, end = _validated_range(request)
        scope, department = _resolved_scope(request)

        return Response(safe_overview(
            granularity=granularity, start=start, end=end,
            scope=scope, department=department,
        ))


class UsageExportView(APIView):
    """GET /api/usage/export/ —— 把区间汇总与三维度明细导出为 UTF-8 CSV

    可见范围与概览页完全一致：管理员可导出全企业，其他人只能导出本部门。
    只导出聚合计数、失败率与平均耗时，不含会话正文、提问内容与个人明细。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        start, end = _validated_range(request)
        _, department = _resolved_scope(request)

        rows = safe_export_rows(start=start, end=end, department=department)
        buffer = io.StringIO()
        buffer.write('\ufeff')  # BOM：让 Excel 正确识别 UTF-8 中文
        writer = csv.writer(buffer)
        writer.writerow(EXPORT_HEADER)
        writer.writerows(rows)

        response = HttpResponse(buffer.getvalue(), content_type='text/csv; charset=utf-8')
        filename = f'usage-{start.isoformat()}-{end.isoformat()}.csv'
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
