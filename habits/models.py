# habits/models.py
from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()

class Habit(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='habists')
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    category = models.CharField(max_length=255)
    current_streak = models.PositiveIntegerField(default=0)
    longest_streak = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    

    class Meta:
        db_table = 'habits'
        ordering = ['-created_at']
        verbose_name = 'Habit'
        verbose_name_plural = 'Habits'
        constraints = [
            models.UniqueConstraint(fields=['user', 'name'], name='unique_user_habit'),
        ]
    
    def __str__(self):
        return f"{self.user.username} - {self.name}"

class HabitRecord(models.Model):
    habit = models.ForeignKey(Habit, on_delete=models.CASCADE, related_name='records')
    data = models.DecimalField(max_digits=10, decimal_places=2)
    date = models.DateField()
    completed = models.BooleanField(default=False)

    class Meta:
        db_table = 'habit_records'
        ordering = ['-date']
        verbose_name = 'Habit Record'
        verbose_name_plural = 'Habit Records'
        constraints = [
            models.UniqueConstraint(fields=['habit', 'date'], name='unique_habit_record'),
        ]
    
    def __str__(self):
        return f"{self.habit.user.username} - {self.habit.name} - {self.date} - {'Completed' if self.completed else 'Not Completed'}"