# habits/urls.py
from django.urls import path
from django.contrib.auth.decorators import login_required
from . import views

app_name = 'habits'

urlpatterns = [
    path('', views.index, name='index'),
    path('list/', login_required(views.habit_list), name='list'),
    path('add/', login_required(views.add_habit), name='add'),
    path('update/<int:habit_id>/', views.UpdateHabitView.as_view(), name='update'),
    path('delete/<int:habit_id>/', views.DeleteHabitView.as_view(), name='delete'),
]