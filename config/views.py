from django.shortcuts import render
from django.views.decorators.csrf import requires_csrf_token


@requires_csrf_token
def csrf_failure(request, reason=''):
    return render(
        request,
        '403_csrf.html',
        {'reason': reason},
        status=403,
    )
