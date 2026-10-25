from django.urls import path

from . import views

app_name = 'habits'

urlpatterns = [
    path('', views.ActivityView.as_view(), name='index'),
    # Service worker pod /habits/, żeby jego zasięg obejmował stronę aktywności.
    path('sw.js', views.ActivityServiceWorkerView.as_view(), name='sw'),
    path('manifest.webmanifest', views.ActivityManifestView.as_view(), name='manifest'),
    path('polaczenia/', views.ConnectionsView.as_view(), name='connections'),
    path('api/snapshot/', views.SnapshotApiView.as_view(), name='api-snapshot'),
    path('api/sync/', views.SyncApiView.as_view(), name='api-sync'),
    path('api/refresh/', views.RefreshApiView.as_view(), name='api-refresh'),
    path('api/route/<str:uid>/', views.RouteApiView.as_view(), name='api-route'),
]
