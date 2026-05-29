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
    path('shopping/', views.ShoppingListView.as_view(), name='shopping-list'),
    path('shopping/create/', views.CreateShoppingListView.as_view(), name='create-shopping-list'),
    path('shopping/auto/', views.GenerateShoppingListView.as_view(), name='generate-shopping-list'),
    path('shopping/<int:list_id>/', views.ShoppingListDetailView.as_view(), name='shopping-list-detail'),
    path('shopping/<int:list_id>/edit/', views.EditShoppingListView.as_view(), name='edit-shopping-list'),
    path('shopping/<int:list_id>/delete/', views.DeleteShoppingListView.as_view(), name='delete-shopping-list'),
    path('shopping/<int:list_id>/items/add/', views.AddShoppingListItemView.as_view(), name='add-shopping-item'),
    path('shopping/<int:list_id>/complete/', views.CompleteShoppingListView.as_view(), name='complete-shopping-list'),
    path('shopping/items/<int:item_id>/update/', views.UpdateShoppingListItemView.as_view(), name='update-shopping-item'),
    path('shopping/items/<int:item_id>/toggle/', views.ToggleShoppingListItemView.as_view(), name='toggle-shopping-item'),
    path('shopping/items/<int:item_id>/delete/', views.DeleteShoppingListItemView.as_view(), name='delete-shopping-item'),
]
