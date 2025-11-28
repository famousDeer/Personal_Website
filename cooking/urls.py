# cooking/urls.py
from django.urls import path
from . import views

app_name = 'cooking'

urlpatterns = [
    path('', views.index, name='index'),
    path('recipes/', views.RecipeListView.as_view(), name='recipe-list'),
    path('recipes/add/', views.AddRecipeView.as_view(), name='add-recipe'),
    path('recipes/edit/<int:recipe_id>/', views.EditRecipeView.as_view(), name='edit-recipe'),
    path('recipes/delete/<int:recipe_id>/', views.DeleteRecipeView.as_view(), name='delete-recipe'),
]