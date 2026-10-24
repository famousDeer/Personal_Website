from django.urls import path

from . import views

app_name = 'habits'

urlpatterns = [
    path('', views.ActivityView.as_view(), name='index'),
    # Service worker pod /habits/, żeby jego zasięg obejmował stronę aktywności.
    path('sw.js', views.ActivityServiceWorkerView.as_view(), name='sw'),
    path('api/snapshot/', views.SnapshotApiView.as_view(), name='api-snapshot'),
    path('api/sync/', views.SyncApiView.as_view(), name='api-sync'),
    path('api/garmin/', views.GarminSyncApiView.as_view(), name='api-garmin'),
]
