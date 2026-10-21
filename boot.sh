#!/bin/sh
# Migrate, then serve every port from lingqu.yaml in one gunicorn worker (DESIGN §11.2).
. venv/bin/activate
until flask deploy; do
    echo "flask deploy failed, retrying in 5 s"
    sleep 5
done
exec gunicorn -c gunicorn.conf.py lingqu:application
