# cooking/urls.py
from django.urls import path
from . import shopping_app_views, views

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
    path('pantry/barcode/lookup/', views.PantryBarcodeLookupView.as_view(), name='pantry-barcode-lookup'),
    path('pantry/barcode/action/', views.PantryBarcodeActionView.as_view(), name='pantry-barcode-action'),
    path('pantry/barcode/register/', views.PantryBarcodeRegisterView.as_view(), name='pantry-barcode-register'),
    path('pantry/catalog/image/<int:entry_id>/', views.PantryCatalogImageView.as_view(), name='pantry-catalog-image'),
    path('pantry/<int:product_id>/image/', views.PantryProductImageView.as_view(), name='pantry-product-image'),
    path('pantry/<int:product_id>/image/upload/', views.PantryProductImageUploadView.as_view(), name='pantry-product-image-upload'),
    path('pantry/<int:product_id>/edit/', views.EditPantryProductView.as_view(), name='edit-pantry-product'),
    path('pantry/<int:product_id>/delete/', views.DeletePantryProductView.as_view(), name='delete-pantry-product'),
    path('pantry/<int:product_id>/movement/', views.PantryMovementView.as_view(), name='pantry-movement'),
    path('shopping/', views.ShoppingListView.as_view(), name='shopping-list'),
    # Tryb zakupów offline (PWA). Service worker leży pod /shopping/app/,
    # więc kontroluje tylko tę część strony.
    path('shopping/app/', shopping_app_views.ShoppingAppView.as_view(), name='shopping-app'),
    path('shopping/app/sw.js', shopping_app_views.ShoppingAppServiceWorkerView.as_view(), name='shopping-app-sw'),
    path(
        'shopping/app/manifest.webmanifest',
        shopping_app_views.ShoppingAppManifestView.as_view(),
        name='shopping-app-manifest',
    ),
    path('shopping/app/api/snapshot/', shopping_app_views.ShoppingSnapshotApiView.as_view(), name='shopping-api-snapshot'),
    path('shopping/app/api/sync/', shopping_app_views.ShoppingSyncApiView.as_view(), name='shopping-api-sync'),
    path(
        'shopping/app/api/push/subscribe/',
        shopping_app_views.ShoppingPushSubscribeApiView.as_view(),
        name='shopping-api-push-subscribe',
    ),
    path(
        'shopping/app/api/push/unsubscribe/',
        shopping_app_views.ShoppingPushUnsubscribeApiView.as_view(),
        name='shopping-api-push-unsubscribe',
    ),
    path(
        'shopping/app/api/lists/<int:list_id>/complete/',
        shopping_app_views.ShoppingCompleteApiView.as_view(),
        name='shopping-api-complete',
    ),
    path('shopping/create/', views.CreateShoppingListView.as_view(), name='create-shopping-list'),
    path('shopping/auto/', views.GenerateShoppingListView.as_view(), name='generate-shopping-list'),
    path('shopping/<int:list_id>/', views.ShoppingListDetailView.as_view(), name='shopping-list-detail'),
    path('shopping/<int:list_id>/edit/', views.EditShoppingListView.as_view(), name='edit-shopping-list'),
    path('shopping/<int:list_id>/delete/', views.DeleteShoppingListView.as_view(), name='delete-shopping-list'),
    path('shopping/<int:list_id>/items/add/', views.AddShoppingListItemView.as_view(), name='add-shopping-item'),
    path(
        'shopping/<int:list_id>/items/pantry/<int:product_id>/',
        views.AddPantryProductToShoppingListView.as_view(),
        name='add-pantry-product-to-shopping-list',
    ),
    path('shopping/<int:list_id>/complete/', views.CompleteShoppingListView.as_view(), name='complete-shopping-list'),
    path('shopping/items/<int:item_id>/update/', views.UpdateShoppingListItemView.as_view(), name='update-shopping-item'),
    path('shopping/items/<int:item_id>/toggle/', views.ToggleShoppingListItemView.as_view(), name='toggle-shopping-item'),
    path('shopping/items/<int:item_id>/delete/', views.DeleteShoppingListItemView.as_view(), name='delete-shopping-item'),
]
