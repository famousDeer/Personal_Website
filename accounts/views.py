# accounts/views.py
from django.shortcuts import render, redirect
from django.contrib.auth import login, authenticate
from django.contrib import messages
from .forms import SignUpForm
from django.views.generic import CreateView
from django.urls import reverse_lazy

def signup(request):
    if request.method == "POST":
        form = SignUpForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, "Konto zostało utworzone, witaj w serwisie")
            return redirect('index')
        else:
            messages.error(request, "Popraw błędy w formularzu.")
    else:
        form = SignUpForm()
    return render(request, "accounts/signup.html", {"form": form})