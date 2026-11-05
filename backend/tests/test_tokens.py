import pytest

from accounts import tokens
from accounts.models import McpToken, Role, User

pytestmark = pytest.mark.django_db


def user(email="a@example.com", role=Role.EDITOR):
    return User.objects.create(username=email, email=email, role=role)


def test_create_authenticate_revoke():
    u = user()
    tok, raw = tokens.create_token(u, "cursor")
    assert raw.startswith("sgl_") and tok.prefix == raw[:10]
    assert raw not in (tok.token_hash, tok.name)           # only a hash is stored
    assert tokens.authenticate(raw) == u
    tok.refresh_from_db()
    assert tok.last_used_at is not None
    tokens.revoke(u, tok.pk)
    assert tokens.authenticate(raw) is None


@pytest.mark.parametrize("bad", ["", None, "sgl_nope", "nope", "Bearer sgl_x"])
def test_unknown_tokens_rejected(bad):
    assert tokens.authenticate(bad) is None


def test_disabled_user_token_rejected():
    u = user()
    _, raw = tokens.create_token(u, "x")
    u.is_active = False
    u.save()
    assert tokens.authenticate(raw) is None


def test_cannot_revoke_someone_elses_token():
    a, b = user("a@example.com"), user("b@example.com")
    tok, raw = tokens.create_token(a, "x")
    with pytest.raises(tokens.TokenError):
        tokens.revoke(b, tok.pk)
    assert tokens.authenticate(raw) == a


def test_token_name_required_and_limit():
    u = user()
    with pytest.raises(tokens.TokenError):
        tokens.create_token(u, "  ")
    for i in range(20):
        tokens.create_token(u, f"t{i}")
    with pytest.raises(tokens.TokenError):
        tokens.create_token(u, "one too many")
    assert McpToken.objects.count() == 20


def test_role_change_applies_to_existing_tokens():
    u = user(role=Role.APPROVER)
    _, raw = tokens.create_token(u, "x")
    u.role = Role.VIEWER
    u.save()
    assert tokens.authenticate(raw).role == Role.VIEWER      # role is read live, never baked into the token
