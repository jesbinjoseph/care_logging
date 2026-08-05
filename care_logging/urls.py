from django.http import HttpResponse
from django.urls import path


def healthy(_request):
    return HttpResponse("OK")


urlpatterns = [
    path("health", healthy),
]
