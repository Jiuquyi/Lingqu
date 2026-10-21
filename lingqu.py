import os

from dotenv import load_dotenv

dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)

COV = None
if os.environ.get("FLASK_COVERAGE"):
    import coverage
    COV = coverage.coverage(branch=True, include="app/*")
    COV.start()

from app import create_app  # noqa: E402
from app.cli import register  # noqa: E402
from app.ports import PortDispatcher  # noqa: E402

app = create_app(os.getenv("FLASK_CONFIG") or "default")
register(app, COV)
# gunicorn serves `lingqu:application`; `flask` commands use `app`.
application = PortDispatcher(app)
application.extensions = app.extensions
application.app = app
