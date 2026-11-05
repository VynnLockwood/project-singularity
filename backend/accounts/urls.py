from django.urls import path

from . import views

urlpatterns = [
    path("csrf", views.csrf),
    path("me", views.me),
    path("auth/google/login", views.google_login),
    path("auth/google/callback", views.google_callback),
    path("auth/logout", views.logout_view),
    path("users", views.users_list),
    path("users/<int:user_id>/role", views.user_role),
]
