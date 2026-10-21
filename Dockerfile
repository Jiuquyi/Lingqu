FROM python:3.12-slim

ENV FLASK_APP=lingqu.py \
    FLASK_CONFIG=production \
    LINGQU_CONFIG=/etc/lingqu/lingqu.yaml \
    PYTHONUNBUFFERED=1

WORKDIR /home/lingqu
COPY requirements requirements
RUN python -m venv venv && venv/bin/pip install --no-cache-dir -r requirements/docker.txt

RUN useradd --create-home --uid 10001 lingqu && mkdir -p /data && chown lingqu /data
COPY app app
COPY migrations migrations
COPY lingqu.py config.py gunicorn.conf.py boot.sh ./
RUN chmod +x boot.sh && chown -R lingqu:lingqu /home/lingqu

USER lingqu
VOLUME /data
EXPOSE 8090 8091
ENTRYPOINT ["./boot.sh"]
