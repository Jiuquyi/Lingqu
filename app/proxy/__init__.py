from flask import Blueprint

proxy = Blueprint("proxy", __name__)

from . import views  # noqa: E402,F401
