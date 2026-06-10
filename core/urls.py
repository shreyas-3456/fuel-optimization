from django.urls import path
from . import views

app_name = 'core'

urlpatterns = [
    path('api/route-fuel/', views.api_route_fuel, name='api_route_fuel'),
]
