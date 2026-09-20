from django.contrib import admin

from .models import (
    PantryMovement,
    PantryProduct,
    ProductCatalogEntry,
    ProductCatalogQuota,
    Recipe,
    RecipeStep,
    RecipeStepIngredient,
    ShoppingList,
    ShoppingListItem,
)


class RecipeStepIngredientInline(admin.TabularInline):
    model = RecipeStepIngredient
    extra = 1


class RecipeStepInline(admin.StackedInline):
    model = RecipeStep
    extra = 1
    show_change_link = True


@admin.register(Recipe)
class RecipeAdmin(admin.ModelAdmin):
    list_display = ('title', 'user', 'meal_type', 'kitchen_region', 'created_at')
    search_fields = ('title', 'ingredients', 'instructions')
    list_filter = ('meal_type', 'kitchen_region', 'type_of_dish')
    inlines = [RecipeStepInline]


@admin.register(RecipeStep)
class RecipeStepAdmin(admin.ModelAdmin):
    list_display = ('recipe', 'order', 'title', 'mix_after', 'duration_minutes')
    list_filter = ('mix_after',)
    search_fields = ('recipe__title', 'title', 'instruction')
    inlines = [RecipeStepIngredientInline]


@admin.register(RecipeStepIngredient)
class RecipeStepIngredientAdmin(admin.ModelAdmin):
    list_display = ('name', 'step', 'quantity', 'unit', 'category')
    search_fields = ('name', 'step__recipe__title')
    list_filter = ('unit', 'category')


@admin.register(PantryProduct)
class PantryProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'barcode', 'quantity_per_scan', 'created_by', 'category', 'current_package_count', 'current_quantity', 'unit', 'minimum_quantity', 'restock_lead_days')
    search_fields = ('name', 'barcode', 'category', 'created_by__username')
    list_filter = ('category', 'unit')


@admin.register(PantryMovement)
class PantryMovementAdmin(admin.ModelAdmin):
    list_display = (
        'product', 'movement_type', 'package_count', 'quantity',
        'stock_was_insufficient', 'occurred_on', 'scan_id', 'created_at',
    )
    search_fields = ('product__name', 'note')
    list_filter = ('movement_type', 'stock_was_insufficient', 'occurred_on')


@admin.register(ProductCatalogEntry)
class ProductCatalogEntryAdmin(admin.ModelAdmin):
    list_display = ('product_name', 'lookup_barcode', 'brand', 'status', 'source', 'fetched_at', 'valid_until')
    search_fields = ('product_name', 'brand', 'lookup_barcode', 'canonical_barcode')
    list_filter = ('source', 'status', 'product_type')
    readonly_fields = ('created_at', 'updated_at', 'fetched_at')


@admin.register(ProductCatalogQuota)
class ProductCatalogQuotaAdmin(admin.ModelAdmin):
    list_display = ('source', 'request_count', 'window_started_at')
    readonly_fields = ('source', 'request_count', 'window_started_at')


class ShoppingListItemInline(admin.TabularInline):
    model = ShoppingListItem
    extra = 1


@admin.register(ShoppingList)
class ShoppingListAdmin(admin.ModelAdmin):
    list_display = ('title', 'created_by', 'source', 'status', 'created_at', 'updated_at')
    search_fields = ('title', 'created_by__username', 'items__name')
    list_filter = ('source', 'status', 'created_at')
    inlines = [ShoppingListItemInline]


@admin.register(ShoppingListItem)
class ShoppingListItemAdmin(admin.ModelAdmin):
    list_display = ('name', 'shopping_list', 'quantity', 'unit', 'category', 'is_purchased')
    search_fields = ('name', 'shopping_list__title', 'note')
    list_filter = ('unit', 'category', 'is_purchased')
