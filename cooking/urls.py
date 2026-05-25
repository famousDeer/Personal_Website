# cooking/urls.py
from django.urls import path
from . import views

app_name = 'cooking'

urlpatterns = [
    path('', views.index, name='index'),
    path('cook/', views.CookView.as_view(), name='cook'),
    path('recipes/', views.RecipeListView.as_view(), name='recipe-list'),
    path('recipes/add/', views.AddRecipeView.as_view(), name='add-recipe'),
    path('recipes/edit/<int:recipe_id>/', views.EditRecipeView.as_view(), name='edit-recipe'),
    path('recipes/delete/<int:recipe_id>/', views.DeleteRecipeView.as_view(), name='delete-recipe'),
    path('pantry/', views.PantryListView.as_view(), name='pantry'),
    path('pantry/add/', views.AddPantryProductView.as_view(), name='add-pantry-product'),
    path('pantry/<int:product_id>/movement/', views.PantryMovementView.as_view(), name='pantry-movement'),
]
