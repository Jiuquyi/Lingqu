import os
import tempfile
import unittest

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from flask_migrate import downgrade, upgrade
from sqlalchemy import inspect

from app import create_app, db
from app.models import Ledger
from tests.helpers import load


class MigrationTestCase(unittest.TestCase):
    def test_m4_upgrade_matches_models_and_downgrade_preserves_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load()
            settings.dsn = "sqlite:///" + os.path.join(directory, "migration.db")
            app = create_app("testing", settings)
            migrations = os.path.abspath("migrations")
            with app.app_context():
                upgrade(directory=migrations, revision="0002")
                db.session.execute(db.text("INSERT INTO ledger (ts, pool, eusd, cache_state) VALUES (1, 'p', 3, 'warm')"))
                db.session.commit()
                upgrade(directory=migrations)
                with db.engine.connect() as connection:
                    differences = compare_metadata(MigrationContext.configure(connection), db.metadata)
                self.assertEqual(differences, [])
                self.assertEqual(Ledger.query.first().eusd, 3)
                downgrade(directory=migrations, revision="0002")
                self.assertNotIn("budget_snapshots", inspect(db.engine).get_table_names())
                self.assertEqual(db.session.execute(db.text("SELECT cache_state FROM ledger")).scalar(), "warm")
                db.session.remove()
                db.engine.dispose()
