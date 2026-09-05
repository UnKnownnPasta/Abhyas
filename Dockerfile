# Abhyas junction interface.
#
# Build context is this directory (Abhyas/) and NOT interactive-ui/, because
# config.py resolves the OSM source to PARENT/net - one level above the
# package. The layout inside the image mirrors the host:
#
#     /app/net/                 OSM source
#     /app/interactive-ui/      the app
#
# so Path(__file__).parent.parent still lands where config.py expects.
#
# docker build -t abhyas:latest .
# docker run --rm -p 8000:8000 abhyas:latest

FROM python:3.11-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# the eclipse-sumo wheel ships netconvert/sumo/sumo-gui binaries built against
# Xerces-C, GCC runtime libs and a full X11/GL stack, none of which the slim
# image carries. Pulling libs one failed binary at a time wastes a full
# rebuild per lib, so install the whole set SUMO's upstream Docker image uses.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates libexpat1 libatomic1 libgomp1 \
      libx11-6 libxext6 libxrender1 libxrandr2 libxfixes3 libxcursor1 \
      libxi6 libsm6 libice6 libgl1 libglu1-mesa \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# deps first so a source edit doesn't re-resolve the whole SUMO stack
COPY interactive-ui/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

WORKDIR /app
COPY net/ /app/net/
COPY interactive-ui/ /app/interactive-ui/

# Build the network here rather than on first boot - makes the image
# self contained and turns a netconvert failure into a failed build instead of
# a container that comes up wrong.
WORKDIR /app/interactive-ui
RUN python -c "from abhyas import netbuild; arms = netbuild.build(); \
print('network built, arms: ' + ', '.join(sorted(arms)))" \
 && test -f build/junction.net.xml

# and fail the build if the model can't reproduce itself
RUN python -m abhyas.selftest


FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl libexpat1 libatomic1 libgomp1 \
      libx11-6 libxext6 libxrender1 libxrandr2 libxfixes3 libxcursor1 \
      libxi6 libsm6 libice6 libgl1 libglu1-mesa \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 abhyas

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder --chown=abhyas:abhyas /app /app

# what the server writes into. declared so they exist and are writable even
# when nothing is mounted over them.
RUN mkdir -p /app/interactive-ui/build/runs \
             /app/interactive-ui/results \
             /app/interactive-ui/results/workflows \
 && chown -R abhyas:abhyas /app/interactive-ui/build \
                           /app/interactive-ui/results

USER abhyas
WORKDIR /app/interactive-ui
EXPOSE 8000

# 0.0.0.0 and dynamically respects $PORT for Render cloud deployment
CMD ["sh", "-c", "python run.py --serve --host 0.0.0.0 --port ${PORT:-8000}"]

# the sim thread starts with the app, so "process is up" isn't the same as
# "the model is running". ask the app itself.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${PORT:-8000}/api/voice/status || exit 1
