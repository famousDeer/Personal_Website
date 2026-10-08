from django.contrib import admin

from .models import (
    PantryCategory,
    PantryCategoryRule,
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
    """Składniki przepisu; krok jest opcjonalny."""
    model = RecipeStepIngredient
    fk_name = 'recipe'
    fields = ('order', 'name', 'quantity', 'unit', 'step', 'category')
    extra = 1

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        # Do wyboru tylko kroki tego przepisu.
        if db_field.name == 'step':
            recipe_id = request.resolver_match.kwargs.get('object_id')
            kwargs['queryset'] = RecipeStep.objects.filter(recipe_id=recipe_id) if recipe_id else RecipeStep.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class RecipeStepInline(admin.StackedInline):
    model = RecipeStep
    extra = 1
    show_change_link = True


@admin.register(Recipe)
class RecipeAdmin(admin.ModelAdmin):
    list_display = ('title', 'user', 'meal_type', 'kitchen_region', 'created_at')
    search_fields = ('title', 'ingredients', 'instructions')
    list_filter = ('meal_type', 'kitchen_region', 'type_of_dish')
    inlines = [RecipeStepIngredientInline, RecipeStepInline]


@admin.register(RecipeStep)
class RecipeStepAdmin(admin.ModelAdmin):
    list_display = ('recipe', 'order', 'title', 'mix_after', 'duration_minutes')
    list_filter = ('mix_after',)
    search_fields = ('recipe__title', 'title', 'instruction')


@admin.register(RecipeStepIngredient)
class RecipeStepIngredientAdmin(admin.ModelAdmin):
    list_display = ('name', 'recipe', 'step', 'quantity', 'unit', 'category')
    search_fields = ('name', 'recipe__title')
    list_filter = ('unit', 'category')


@admin.register(PantryProduct)
class PantryProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'barcode', 'quantity_per_scan', 'created_by', 'category', 'current_package_count', 'current_quantity', 'unit', 'minimum_quantity', 'restock_lead_days', 'one_off')
    search_fields = ('name', 'barcode', 'category', 'created_by__username')
    list_filter = ('category', 'unit', 'one_off')


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


class PantryCategoryRuleInline(admin.TabularInline):
    model = PantryCategoryRule
    extra = 0
    fields = ('kind', 'position', 'patterns')


@admin.register(PantryCategory)
class PantryCategoryAdmin(admin.ModelAdmin):
    """Zwykła edycja jest na stronie "Kategorie" w spiżarni - tam zmiana nazwy
    przepisuje ją też w produktach i listach. Zmiana nazwy tutaj tego nie robi."""

    list_display = ('name', 'group', 'position', 'code')
    list_filter = ('group',)
    ordering = ('group', 'position')
    inlines = [PantryCategoryRuleInline]
