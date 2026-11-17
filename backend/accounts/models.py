from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models.functions import Lower


class Role(models.TextChoices):
    VIEWER = "viewer", "Viewer"      # read
    EDITOR = "editor", "Editor"      # read + edit
    APPROVER = "approver", "Approver"  # read + edit + approve
    ADMIN = "admin", "Admin"  # approver + manage users and access


ROLE_RANK = {Role.VIEWER: 0, Role.EDITOR: 1, Role.APPROVER: 2, Role.ADMIN: 3}


class User(AbstractUser):
    email = models.EmailField(unique=True)
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.VIEWER)

    class Meta(AbstractUser.Meta):
        # emails are stored trimmed and lower-cased (accounts.services.normalize_email); this keeps
        # "Nolan@Gmail.com" and "nolan@gmail.com" from ever being two users, whoever writes the row
        constraints = [models.UniqueConstraint(Lower("email"), name="accounts_user_email_ci_unique")]

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

    @property
    def can_admin(self) -> bool:
        return self.rank >= ROLE_RANK[Role.ADMIN]


GRANT_ROLES = (Role.VIEWER, Role.EDITOR, Role.APPROVER)  # admin is global, never a grant


class SchemaGrant(models.Model):
    """A user's level on one database (a managed Postgres schema), or on all of them with schema `*`.

    The user's global role caps it; `accounts.access` is the only place that reads grants to decide.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="schema_grants")
    schema = models.CharField(max_length=63)  # a managed database name, or "*" (all, including later ones)
    role = models.CharField(max_length=16, choices=[(r.value, r.label) for r in GRANT_ROLES])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["user_id", "schema"]
        constraints = [
            models.UniqueConstraint(fields=["user", "schema"], name="accounts_schemagrant_user_schema_unique"),
            models.CheckConstraint(condition=models.Q(role__in=[r.value for r in GRANT_ROLES]),
                                   name="accounts_schemagrant_role_valid"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.schema}={self.role}"


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
    # OAuth tokens only: the access token expires and is renewed with the refresh token (manual tokens never expire)
    expires_at = models.DateTimeField(null=True, blank=True)
    refresh_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)

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
