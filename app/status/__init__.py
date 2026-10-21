from flask import Blueprint

status = Blueprint("status", __name__, template_folder="templates")

from . import views  # noqa: E402,F401

from . import console
