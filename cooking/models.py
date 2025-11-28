# cooking/models.py
from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()

# Recipe database
class Recipe(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='recipes')
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    ingredients = models.TextField()
    instructions = models.TextField()
    portions = models.PositiveIntegerField(default=1)
    kcal = models.PositiveIntegerField(default=0)
    preparation_time = models.PositiveIntegerField(help_text="Preparation time in minutes", default=5)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    kitchen_region = models.CharField(max_length=100, blank=True)
    meal_type = models.CharField(max_length=100, blank=True)
    type_of_dish = models.CharField(max_length=100, blank=True)
    image = models.ImageField(upload_to='recipes_img', blank=True, null=True)

    class Meta:
        db_table = 'recipes'
        ordering = ['-created_at']
        verbose_name = "Recipe"
        verbose_name_plural = "Recipes"
    
    def __str__(self):
        return f"{self.title} by {self.user.username}"