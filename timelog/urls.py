from django.urls import path

from . import views

app_name = 'timelog'

urlpatterns = [
    path('', views.DayView.as_view(), name='index'),
    path('dzien/<str:day>/', views.DayView.as_view(), name='day'),
    path('start/', views.StartView.as_view(), name='start'),
    path('stop/', views.StopView.as_view(), name='stop'),
    path('wpis/dodaj/', views.EntryCreateView.as_view(), name='entry-add'),
    path('wpis/<int:pk>/', views.EntryEditView.as_view(), name='entry'),
    path('wpis/<int:pk>/usun/', views.EntryDeleteView.as_view(), name='entry-delete'),
    path('podsumowanie/', views.SummaryView.as_view(), name='summary'),
    path('kategorie/', views.CategoriesView.as_view(), name='categories'),
    path('automatyzacja/', views.AutomationView.as_view(), name='automation'),
    # Skróty iOS: klucz w nagłówku Authorization: Bearer <klucz>.
    path('api/start/', views.ApiStartView.as_view(), name='api-start'),
    path('api/stop/', views.ApiStopView.as_view(), name='api-stop'),
    path('api/status/', views.ApiStatusView.as_view(), name='api-status'),
]
