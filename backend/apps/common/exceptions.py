from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status


def custom_exception_handler(exc, context):
    """自定义异常处理，统一错误响应格式"""

    response = exception_handler(exc, context)

    if response is not None:
        # 统一错误格式
        response.data = {
            'detail': response.data.get('detail', str(exc)) if isinstance(response.data, dict) else response.data,
            'code': response.status_code,
        }
    else:
        # 未被 DRF 捕获的异常
        response = Response(
            {
                'detail': str(exc),
                'code': status.HTTP_500_INTERNAL_SERVER_ERROR,
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return response
