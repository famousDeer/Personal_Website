# cars/urls.py
from django.urls import path
from . import views

app_name = 'cars'

urlpatterns = [
    path('', views.GarageView.as_view(), name='garage'),
    path('add/', views.AddCarView.as_view(), name='add_car'),
    path('dashboard/<int:car_id>/', views.CarDashboardView.as_view(), name='dashboard'),
    path('dashboard/<int:car_id>/add-fuel/', views.AddFuelView.as_view(), name='add_fuel'),
]