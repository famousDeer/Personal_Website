# accounts/urls.py
from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    path("signup/", views.signup, name="signup"),
    path("profile/", views.profile, name="profile"),
    path("shared-accounts/<int:account_id>/edit/", views.edit_shared_account, name="edit_shared_account"),
    path("shared-accounts/<int:account_id>/delete/", views.delete_shared_account, name="delete_shared_account"),
    path("login/", auth_views.LoginView.as_view(
            template_name="accounts/login.html",
            redirect_authenticated_user=True
        ), name="login"),
    path("logout/", auth_views.LogoutView.as_view(
            template_name="accounts/logout.html",
            next_page=None
    ), name="logout"),
]
