import os

from django.core.management.base import BaseCommand

from intake import run


class Command(BaseCommand):
    help = "Run an import (started by the web app); its state and thread are kept on the run row."

    def add_arguments(self, parser):
        parser.add_argument("run_id", type=int)

    def handle(self, *args, **opts):
        # the chat API key arrives only in the environment, and is taken out of it at once
        run.execute(opts["run_id"], os.environ.pop(run.KEY_ENV, ""))
