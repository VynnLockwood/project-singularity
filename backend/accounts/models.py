from django.contrib.auth.models import AbstractUser
from django.db import models


class Role(models.TextChoices):
    VIEWER = "viewer", "Viewer"      # read
    EDITOR = "editor", "Editor"      # read + edit
    APPROVER = "approver", "Approver"  # read + edit + approve


ROLE_RANK = {Role.VIEWER: 0, Role.EDITOR: 1, Role.APPROVER: 2}


class User(AbstractUser):
    email = models.EmailField(unique=True)
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.VIEWER)

    @property
    def rank(self) -> int:
        return ROLE_RANK[Role(self.role)]

    @property
    def can_read(self) -> bool:
        return self.rank >= ROLE_RANK[Role.VIEWER]

    @property
    def can_edit(self) -> bool:
        return self.rank >= ROLE_RANK[Role.EDITOR]

    @property
    def can_approve(self) -> bool:
        return self.rank >= ROLE_RANK[Role.APPROVER]
