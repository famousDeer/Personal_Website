"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include, re_path
from django.views.generic import TemplateView
from django.views.static import serve
from django.conf import settings

urlpatterns = [
    path('', TemplateView.as_view(template_name='home.html'), name='index'),
    path('admin/', admin.site.urls),
    path('finance/', include('finance.urls')),
    path('accounts/', include('accounts.urls')),
    path('habits/', include('habits.urls')),
    path('cooking/', include('cooking.urls')),
    path('cars/', include('cars.urls')),
]

# Publiczne media (zdjęcia przepisów, obrazki z katalogu produktów) muszą być
# dostępne także przy DEBUG=0 - inaczej wyłączenie DEBUG na Pi gasi wszystkie
# zdjęcia. Pomocnik django.conf.urls.static.static() zwraca pustą listę, gdy
# DEBUG jest wyłączone, więc rejestrujemy trasę wprost.
# Ruch jest niewielki (kilkadziesiąt obrazków w sieci domowej). Jeśli kiedyś
# postawisz reverse proxy, przechwyć /media/ w nim i ta trasa przestanie być
# używana. Prywatne zdjęcia produktów NIE idą tą drogą - są serwowane
# widokami z kontrolą właściciela (cooking.views.PantryProductImageView).
urlpatterns += [
    re_path(
        r'^%s(?P<path>.*)$' % settings.MEDIA_URL.lstrip('/'),
        serve,
        {'document_root': settings.MEDIA_ROOT},
    ),
]
