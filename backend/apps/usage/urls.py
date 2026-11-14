from django.urls import path

from backend.apps.usage.views import UsageExportView, UsageOverviewView

urlpatterns = [
    path('overview/', UsageOverviewView.as_view(), name='usage-overview'),
    path('export/', UsageExportView.as_view(), name='usage-export'),
]
