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


class McpToken(models.Model):
    """A personal access token for MCP clients (Claude Desktop, Cursor, ...).

    Only a hash is stored. The raw token is shown once, when it is created.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="mcp_tokens")
    name = models.CharField(max_length=64)
    token_hash = models.CharField(max_length=64, unique=True)
    prefix = models.CharField(max_length=12)  # first characters, so people can tell tokens apart
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class OAuthClient(models.Model):
    """An MCP client (Claude, Gemini, ...) that registered itself (RFC 7591)."""

    client_id = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=100)
    redirect_uris = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)


class OAuthCode(models.Model):
    """A one-time authorization code, bound to the client, the redirect and the PKCE challenge."""

    code_hash = models.CharField(max_length=64, unique=True)
    client = models.ForeignKey(OAuthClient, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    redirect_uri = models.TextField()
    code_challenge = models.CharField(max_length=128)
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)
