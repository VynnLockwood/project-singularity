"""Emails become case-insensitively unique and are stored in their canonical form (trimmed, lower-case).

If two existing users share an email apart from case or spaces, the migration stops and names them: an admin
decides which account to keep. Nothing is deleted or merged here.
"""
from django.db import migrations, models
from django.db.models.functions import Lower


def normalize_emails(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    seen: dict[str, list[int]] = {}
    for pk, email in User.objects.values_list("pk", "email"):
        seen.setdefault((email or "").strip().lower(), []).append(pk)
    clashes = {e: pks for e, pks in seen.items() if len(pks) > 1}
    if clashes:
        listed = "; ".join(f"{e or '(empty)'}: user ids {pks}" for e, pks in sorted(clashes.items()))
        raise RuntimeError(
            "accounts.0006: these users share an email apart from case or spaces, so emails cannot be made unique. "
            "Turn off and rename the extra accounts (nothing was changed), then migrate again: " + listed
        )
    for user in User.objects.all().only("pk", "email"):
        canonical = (user.email or "").strip().lower()
        if canonical != user.email:
            User.objects.filter(pk=user.pk).update(email=canonical)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0005_admin_role"),
    ]

    operations = [
        migrations.RunPython(normalize_emails, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.UniqueConstraint(Lower("email"), name="accounts_user_email_ci_unique"),
        ),
    ]
