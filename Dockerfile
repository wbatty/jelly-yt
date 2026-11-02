FROM python:3.12-slim

# yt-dlp needs a JavaScript runtime to solve YouTube's player challenges
# (without it only a degraded set of formats is available).
COPY --from=denoland/deno:bin-2.5.6 /deno /usr/local/bin/deno

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY jelly_yt ./jelly_yt
RUN pip install --no-deps .

# Match the UID/GID that owns MEDIA_ROOT on the host (and that Jellyfin can read).
ARG UID=1000
ARG GID=1000
RUN groupadd -g ${GID} app && useradd -u ${UID} -g ${GID} -M -s /usr/sbin/nologin app
USER app

EXPOSE 8080
CMD ["python", "-m", "jelly_yt.proxy"]
