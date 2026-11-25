from django.urls import path

from .views import ChoreDeleteView, ChoreEditView, ChoreListView, ChoreStatusView

app_name = 'chores'

urlpatterns = [
    path('', ChoreListView.as_view(), name='index'),
    path('dodaj/', ChoreEditView.as_view(), name='add'),
    path('<int:pk>/edytuj/', ChoreEditView.as_view(), name='edit'),
    path('<int:pk>/stan/', ChoreStatusView.as_view(), name='status'),
    path('<int:pk>/usun/', ChoreDeleteView.as_view(), name='delete'),
]
