# cars/urls.py
from django.urls import path
from . import views

app_name = 'cars'

urlpatterns = [
    path('', views.GarageView.as_view(), name='garage'),
    path('add/', views.AddCarView.as_view(), name='add_car'),
    path('dashboard/<int:car_id>/', views.CarDashboardView.as_view(), name='dashboard'),
    path('dashboard/<int:car_id>/add-fuel/', views.AddFuelView.as_view(), name='add_fuel'),
    path('dashboard/<int:car_id>/edit-fuel/<int:fuel_id>/', views.EditFuelView.as_view(), name='edit_fuel'),
    path('dashboard/<int:car_id>/delete-fuel/<int:fuel_id>/', views.DeleteFuelView.as_view(), name='delete_fuel'),
    path('dashboard/<int:car_id>/delete/', views.DeleteCarView.as_view(), name='delete'),
    path('dashboard/<int:car_id>/edit/', views.EditCarView.as_view(), name='edit'),
    path('dashboard/<int:car_id>/add-service/', views.AddServiceView.as_view(), name='add_service'),
    path('dashboard/<int:car_id>/edit-service/<int:service_id>/', views.EditServiceView.as_view(), name='edit_service'),
    path('dashboard/<int:car_id>/delete-service/<int:service_id>/', views.DeleteServiceView.as_view(), name='delete_service'),
    path('dashboard/<int:car_id>/add-tyres/', views.AddTyresView.as_view(), name='add_tyres'),
    path('dashboard/<int:car_id>/edit-tyres/<int:tyre_id>/', views.EditTyresView.as_view(), name='edit_tyres'),
    path('dashboard/<int:car_id>/delete-tyres/<int:tyre_id>/', views.DeleteTyresView.as_view(), name='delete_tyres'),
]