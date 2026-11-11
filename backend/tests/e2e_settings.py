"""Settings for a real child process started by an end-to-end test: the same app, pointed at the test databases."""
import os

from config.settings import *  # noqa: F401,F403
from config.settings import DATABASES, MASTER_DB
from tests.pgsetup import PASSWORDS

DATABASES["default"]["NAME"] = os.environ["E2E_APP_DB"]
MASTER_DB = {**MASTER_DB, "dbname": os.environ["E2E_MASTER_DB"],  # noqa: F811
             "ro_password": PASSWORDS["singularity_master_ro"], "rw_password": PASSWORDS["singularity_master_rw"]}
