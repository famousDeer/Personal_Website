# accounts/views.py
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render

from finance.account_utils import ensure_personal_finance_account
from finance.models import FinanceAccount

from .forms import ProfileUpdateForm, SharedAccountCreateForm, SharedAccountUpdateForm, SignUpForm


def can_manage_shared_account_membership(shared_account, user):
    return shared_account.owner_id in (None, user.id)

def signup(request):
    if request.method == "POST":
        form = SignUpForm(request.POST)
        if form.is_valid():
            user = form.save()
            ensure_personal_finance_account(user)
            login(request, user)
            messages.success(request, "Konto zostało utworzone, witaj w serwisie")
            return redirect('index')
        else:
            messages.error(request, "Popraw błędy w formularzu.")
    else:
        form = SignUpForm()
    return render(request, "accounts/signup.html", {"form": form})


@login_required
@transaction.atomic
def profile(request):
    if request.method == "POST":
        if request.POST.get('form_name') == 'profile':
            profile_form = ProfileUpdateForm(request.POST, instance=request.user)
            shared_account_form = SharedAccountCreateForm(current_user=request.user)
            if profile_form.is_valid():
                profile_form.save()
                messages.success(request, 'Dane konta zostały zaktualizowane.')
                return redirect('profile')
            messages.error(request, 'Popraw błędy w formularzu profilu.')
        elif request.POST.get('form_name') == 'shared_account':
            profile_form = ProfileUpdateForm(instance=request.user)
            shared_account_form = SharedAccountCreateForm(request.POST, current_user=request.user)
            if shared_account_form.is_valid():
                partner = shared_account_form.cleaned_data['partner']
                shared_account = FinanceAccount.objects.create(
                    name=shared_account_form.cleaned_data['name'],
                    account_type=FinanceAccount.SHARED,
                    owner=request.user,
                )
                shared_account.members.add(request.user, partner)
                messages.success(
                    request,
                    f'Utworzono konto wspólne "{shared_account.name}" dla {request.user.username} i {partner.username}.'
                )
                return redirect('profile')
            messages.error(request, 'Nie udało się utworzyć konta wspólnego.')
        else:
            profile_form = ProfileUpdateForm(instance=request.user)
            shared_account_form = SharedAccountCreateForm(current_user=request.user)
    else:
        profile_form = ProfileUpdateForm(instance=request.user)
        shared_account_form = SharedAccountCreateForm(current_user=request.user)

    shared_accounts = FinanceAccount.objects.filter(
        account_type=FinanceAccount.SHARED,
        members=request.user,
    ).prefetch_related('members')

    return render(
        request,
        'accounts/profile.html',
        {
            'profile_form': profile_form,
            'shared_account_form': shared_account_form,
            'shared_accounts': shared_accounts,
        },
    )


@login_required
@transaction.atomic
def edit_shared_account(request, account_id):
    shared_account = get_object_or_404(
        FinanceAccount.objects.prefetch_related('members'),
        id=account_id,
        account_type=FinanceAccount.SHARED,
        members=request.user,
    )

    can_manage_membership = can_manage_shared_account_membership(shared_account, request.user)

    if request.method == 'POST':
        form = SharedAccountUpdateForm(
            request.POST,
            instance=shared_account,
            current_user=request.user,
            can_manage_membership=can_manage_membership,
        )
        if form.is_valid():
            shared_account = form.save(commit=False)
            if shared_account.owner_id is None and can_manage_membership:
                shared_account.owner = request.user
            shared_account.save()

            if can_manage_membership and 'partner' in form.cleaned_data:
                partner = form.cleaned_data['partner']
                shared_account.members.set([request.user, partner])
            messages.success(request, 'Konto wspólne zostało zaktualizowane.')
            return redirect('profile')
        messages.error(request, 'Popraw błędy w formularzu konta wspólnego.')
    else:
        form = SharedAccountUpdateForm(
            instance=shared_account,
            current_user=request.user,
            can_manage_membership=can_manage_membership,
        )

    return render(
        request,
        'accounts/edit_shared_account.html',
        {
            'form': form,
            'shared_account': shared_account,
            'can_manage_membership': can_manage_membership,
        },
    )


@login_required
@transaction.atomic
def delete_shared_account(request, account_id):
    shared_account = get_object_or_404(
        FinanceAccount,
        id=account_id,
        account_type=FinanceAccount.SHARED,
        members=request.user,
    )

    if request.method != 'POST':
        return redirect('edit_shared_account', account_id=shared_account.id)

    if not can_manage_shared_account_membership(shared_account, request.user):
        messages.error(request, 'Tylko twórca konta wspólnego może je usunąć.')
        return redirect('edit_shared_account', account_id=shared_account.id)

    account_name = shared_account.name
    shared_account.delete()
    messages.success(request, f'Konto wspólne "{account_name}" zostało usunięte.')
    return redirect('profile')
