from rest_framework.pagination import PageNumberPagination


class StandardPagination(PageNumberPagination):
    """标准分页"""
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100

    def get_paginated_response(self, data):
        from rest_framework.response import Response
        return Response({
            'count': self.page.paginator.count,
            'page': self.page.number,
            'page_size': self.page_size,
            'total_pages': self.page.paginator.num_pages,
            'results': data,
        })
