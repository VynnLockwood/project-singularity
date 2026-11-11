from django.core.management.base import BaseCommand

from scripts import jobs


class Command(BaseCommand):
    help = "Validate a stored script in the background (started by the web app); progress is kept on the script row."

    def add_arguments(self, parser):
        parser.add_argument("script_id", type=int)

    def handle(self, *args, **opts):
        jobs.run(opts["script_id"])
