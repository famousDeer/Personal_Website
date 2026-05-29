from django.contrib import admin

from .models import (
    PantryMovement,
    PantryProduct,
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
    list_display = ('name', 'user', 'category', 'current_quantity', 'unit', 'minimum_quantity', 'restock_lead_days')
    search_fields = ('name', 'category', 'user__username')
    list_filter = ('category', 'unit')


@admin.register(PantryMovement)
class PantryMovementAdmin(admin.ModelAdmin):
    list_display = ('product', 'movement_type', 'quantity', 'occurred_on', 'created_at')
    search_fields = ('product__name', 'note')
    list_filter = ('movement_type', 'occurred_on')


class ShoppingListItemInline(admin.TabularInline):
    model = ShoppingListItem
    extra = 1


@admin.register(ShoppingList)
class ShoppingListAdmin(admin.ModelAdmin):
    list_display = ('title', 'user', 'source', 'status', 'created_at', 'updated_at')
    search_fields = ('title', 'user__username', 'items__name')
    list_filter = ('source', 'status', 'created_at')
    inlines = [ShoppingListItemInline]


@admin.register(ShoppingListItem)
class ShoppingListItemAdmin(admin.ModelAdmin):
    list_display = ('name', 'shopping_list', 'quantity', 'unit', 'category', 'is_purchased')
    search_fields = ('name', 'shopping_list__title', 'note')
    list_filter = ('unit', 'category', 'is_purchased')
