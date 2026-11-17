"""Per-database access: the SchemaGrant table, and a `*` grant for every existing non-admin user.

Deploy changes nobody's reach: each non-admin user (active or not) gets a grant on all databases at their
current global role. Admins need no grant. Going back drops the table and with it every grant.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


GRANT_ROLES = ("viewer", "editor", "approver")


def grant_existing_users(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    SchemaGrant = apps.get_model("accounts", "SchemaGrant")
    SchemaGrant.objects.bulk_create(
        [SchemaGrant(user_id=pk, schema="*", role=role)
         for pk, role in User.objects.filter(role__in=GRANT_ROLES).values_list("pk", "role")],
        ignore_conflicts=True)


def remove_grants(apps, schema_editor):
    apps.get_model("accounts", "SchemaGrant").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0006_email_ci_unique"),
    ]

    operations = [
        migrations.CreateModel(
            name="SchemaGrant",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("schema", models.CharField(max_length=63)),
                (
                    "role",
                    models.CharField(
                        choices=[
                            ("viewer", "Viewer"),
                            ("editor", "Editor"),
                            ("approver", "Approver"),
                        ],
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="schema_grants",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["user_id", "schema"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("user", "schema"),
                        name="accounts_schemagrant_user_schema_unique",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("role__in", ["viewer", "editor", "approver"])
                        ),
                        name="accounts_schemagrant_role_valid",
                    ),
                ],
            },
        ),
        migrations.RunPython(grant_existing_users, remove_grants),
    ]
