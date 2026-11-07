# Tableau de bord BMW CarData en continu (commande « stream ») dans un conteneur.
# Les données personnelles (configuration, jetons, base de l'historique) restent dans le
# volume /data : elles ne font jamais partie de l'image.
FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin bmw \
 && mkdir /data && chown bmw /data

WORKDIR /app
COPY --chmod=644 requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt
COPY --chmod=644 bmw_cardata.py dashboard_template.html catalogue_bmw.json ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Paris \
    BMW_DATA_DIR=/data \
    BMW_LIVE_HOST=0.0.0.0 \
    BMW_LIVE_PORT=8765

USER bmw
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/live.json', timeout=4)"

CMD ["python", "bmw_cardata.py", "stream"]
