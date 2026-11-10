"""Add the careWithDeletes rule to installs that already have rules (new installs get it from the seeding)."""
from django.db import migrations

RULE = {
    "name": "careWithDeletes",
    "type": "forbidden",
    "params": {"ops": ["delete_rows"]},
    "severity": "warn",
    "description": "Deleting rows destroys data. Check the reason, the row count and that a backup exists.",
}


def add_rule(apps, schema_editor):
    Rule = apps.get_model("settings_app", "Rule")
    RuleVersion = apps.get_model("settings_app", "RuleVersion")
    if not Rule.objects.exists() or Rule.objects.filter(name=RULE["name"]).exists():
        return
    rule = Rule.objects.create(**RULE)
    RuleVersion.objects.create(
        rule=rule, version=rule.version, action="created",
        snapshot={**RULE, "enabled": True},
    )


class Migration(migrations.Migration):

    dependencies = [("settings_app", "0002_setting")]

    operations = [migrations.RunPython(add_rule, migrations.RunPython.noop)]
