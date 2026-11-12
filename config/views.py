from django.shortcuts import render
from django.views.decorators.csrf import requires_csrf_token

from config.home import home_summary


@requires_csrf_token
def csrf_failure(request, reason=''):
    return render(
        request,
        '403_csrf.html',
        {'reason': reason},
        status=403,
    )


def home(request):
    context = {}
    if request.user.is_authenticated:
        context['summary'] = home_summary(request)
    return render(request, 'home.html', context)
