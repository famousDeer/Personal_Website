from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import F
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from cooking.services.push import push_enabled
from utils.undo import delete_with_undo

from .forms import ChoreForm
from .models import Chore


class ChoreListView(LoginRequiredMixin, View):
    def get(self, request):
        view = request.GET.get('widok')
        chores = Chore.objects.prefetch_related('assignees')
        if view == 'wykonane':
            chores = chores.filter(completed_at__isnull=False).order_by('-completed_at', '-pk')
        else:
            chores = chores.filter(completed_at__isnull=True).order_by(
                F('due_on').asc(nulls_last=True), '-pk',
            )
            if view == 'moje':
                chores = chores.filter(assignees=request.user)
            else:
                view = 'otwarte'
        return render(request, 'chores/list.html', {
            'chores': chores,
            'view': view,
            'today': timezone.localdate(),
            'push_available': push_enabled(),
        })


class ChoreEditView(LoginRequiredMixin, View):
    def get(self, request, pk=None):
        chore = get_object_or_404(Chore, pk=pk) if pk is not None else None
        form = ChoreForm(instance=chore, initial={'assignees': [request.user.pk]} if chore is None else None)
        return render(request, 'chores/form.html', {'form': form, 'chore': chore})

    def post(self, request, pk=None):
        with transaction.atomic():
            chore = get_object_or_404(Chore.objects.select_for_update(), pk=pk) if pk is not None else None
            form = ChoreForm(request.POST, instance=chore)
            if form.is_valid():
                form.save()
                messages.success(request, 'Zapisano zadanie.' if chore else 'Dodano zadanie.')
                return redirect('chores:index')
        return render(request, 'chores/form.html', {'form': form, 'chore': chore})


class ChoreStatusView(LoginRequiredMixin, View):
    def post(self, request, pk):
        action = request.POST.get('stan')
        if action not in ('wykonane', 'otwarte'):
            return HttpResponseBadRequest('Nieprawidłowy stan zadania.')

        chore = get_object_or_404(Chore, pk=pk)
        complete = action == 'wykonane'
        Chore.objects.filter(pk=chore.pk, completed_at__isnull=complete).update(
            completed_at=timezone.now() if complete else None,
        )
        messages.success(request, 'Odhaczono zadanie.' if complete else 'Przywrócono zadanie do zrobienia.')
        if complete and request.POST.get('widok') == 'moje':
            return redirect(reverse('chores:index', query={'widok': 'moje'}))
        return redirect('chores:index')


class ChoreDeleteView(LoginRequiredMixin, View):
    @transaction.atomic
    def post(self, request, pk):
        chore = get_object_or_404(Chore.objects.select_for_update(), pk=pk)
        delete_with_undo(request, chore, 'Usunięto zadanie.')
        return redirect('chores:index')
