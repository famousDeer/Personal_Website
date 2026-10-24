from django.contrib import admin

from .models import ActivitySettings, Workout, WorkoutSet


class WorkoutSetInline(admin.TabularInline):
    model = WorkoutSet
    extra = 0


@admin.register(Workout)
class WorkoutAdmin(admin.ModelAdmin):
    list_display = ('date', 'kind', 'title', 'user', 'source', 'is_deleted')
    list_filter = ('kind', 'source', 'is_deleted')
    inlines = [WorkoutSetInline]


admin.site.register(ActivitySettings)
