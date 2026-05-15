from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import Recipe


User = get_user_model()


class RecipeViewsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='owner', password='pass12345')
        self.other = User.objects.create_user(username='other', password='pass12345')
        self.owner_recipe = Recipe.objects.create(
            user=self.owner,
            title='Owner pasta',
            ingredients='<strong>Makaron</strong><script>alert(1)</script>',
            instructions='<a href="javascript:alert(1)">klik</a><p>Gotuj 10 minut.</p>',
            portions=2,
            kcal=500,
            preparation_time=20,
        )
        self.other_recipe = Recipe.objects.create(
            user=self.other,
            title='Other soup',
            ingredients='Woda',
            instructions='Gotuj',
            portions=1,
            kcal=100,
            preparation_time=10,
        )

    def test_recipe_list_is_public_for_authenticated_users(self):
        self.client.login(username='owner', password='pass12345')

        response = self.client.get(reverse('cooking:recipe-list'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Owner pasta')
        self.assertContains(response, 'Other soup')

    def test_recipe_html_is_sanitized_on_list(self):
        self.client.login(username='owner', password='pass12345')

        response = self.client.get(reverse('cooking:recipe-list'))

        self.assertNotContains(response, '<script>alert')
        self.assertNotContains(response, 'javascript:alert')
        self.assertContains(response, '<strong>Makaron</strong>', html=True)

    def test_non_owner_cannot_edit_or_delete_recipe(self):
        self.client.login(username='other', password='pass12345')

        edit_response = self.client.get(reverse('cooking:edit-recipe', args=[self.owner_recipe.id]))
        delete_response = self.client.post(reverse('cooking:delete-recipe', args=[self.owner_recipe.id]))

        self.assertEqual(edit_response.status_code, 404)
        self.assertEqual(delete_response.status_code, 404)
        self.assertTrue(Recipe.objects.filter(id=self.owner_recipe.id).exists())
