from django import forms
from django.contrib.auth import get_user_model

from .models import Chore


class ChoreForm(forms.ModelForm):
    class Meta:
        model = Chore
        fields = ['title', 'notes', 'due_on', 'assignees']
        widgets = {
            'title': forms.TextInput(attrs={'class': 'form-control'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'due_on': forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date', 'class': 'form-control'}),
            'assignees': forms.CheckboxSelectMultiple,
        }
        help_texts = {
            'due_on': 'Opcjonalny. Przypomnienia zaczynają się w dniu terminu.',
            'assignees': 'Wybierz co najmniej jedną osobę.',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['assignees'].queryset = get_user_model().objects.filter(is_active=True).order_by('username')
        self.fields['due_on'].input_formats = ['%Y-%m-%d']
