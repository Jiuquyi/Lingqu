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

    def test_0001_ledger_upgrades_to_0005_and_restores(self):
        """An instance from before M3 (revision 0001) upgraded in one step: its rows have no
        request id, are restored as before, and 0005 can be rolled back without losing them."""
        import time
        from app.gate.hold import Gate
        from app.ledger import restore_counters
        with tempfile.TemporaryDirectory() as directory:
            settings = load()
            settings.dsn = "sqlite:///" + os.path.join(directory, "migration.db")
            app = create_app("testing", settings)
            migrations = os.path.abspath("migrations")
            with app.app_context():
                upgrade(directory=migrations, revision="0001")
                now = time.time()
                for i in range(3):
                    db.session.execute(db.text(
                        "INSERT INTO ledger (ts, pool, model_req, eusd, eusd_input, counted_at, held_ms, duration_ms) "
                        "VALUES (:ts, 'a/claude', 'claude-opus-5-5', 2, 1, :ts, 0, 1000)"), {"ts": now - 600 + i})
                db.session.commit()
                upgrade(directory=migrations)
                self.assertEqual(db.session.execute(db.text("SELECT version_num FROM alembic_version")).scalar(), "0005")
                self.assertEqual(Ledger.query.filter(Ledger.request_id.is_(None)).count(), 3)
                g = Gate(settings)
                restore_counters(app, g)
                self.assertAlmostEqual(g.counters.spent("a/claude", "*", time.time()), 6)
                downgrade(directory=migrations, revision="0004")
                self.assertNotIn("meter_events", inspect(db.engine).get_table_names())
                self.assertEqual(db.session.execute(db.text("SELECT COUNT(*) FROM ledger")).scalar(), 3)
                # Back to the image that knows only 0001: its `flask deploy` must find its own head.
                downgrade(directory=migrations, revision="0001")
                self.assertEqual(db.session.execute(db.text("SELECT version_num FROM alembic_version")).scalar(), "0001")
                self.assertEqual(db.session.execute(db.text("SELECT SUM(eusd) FROM ledger")).scalar(), 6)
                db.session.remove()
                db.engine.dispose()
