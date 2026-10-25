from django.contrib import admin

from .models import Category, Entry, TimelogSettings


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'value', 'auto', 'is_archived')
    list_filter = ('value', 'is_archived')


@admin.register(Entry)
class EntryAdmin(admin.ModelAdmin):
    list_display = ('start', 'end', 'category', 'user', 'source')
    list_filter = ('source',)


admin.site.register(TimelogSettings)
