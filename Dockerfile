# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

FROM python:3.13-slim

# Git is needed by the git extractor; ca-certificates is needed for HTTPS
# to api.github.com and github.com.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Matplotlib's font-cache directory; default $HOME/.config/matplotlib
# isn't writable when --user overrides to a UID without an /etc/passwd
# entry. /tmp is universally writable.
ENV MPLCONFIGDIR=/tmp/matplotlib

# Install Python dependencies first so changes to source code don't
# invalidate the dependency layer.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the source tree and tests.
COPY src /app/src
COPY tests /app/tests

# Expected runtime layout (provided by bind mounts at `docker run`):
#   /app/src         -- source code (in image)
#   /config          -- config.yaml lives here, bind-mounted from host
#   /data            -- sqlite database and gh_runs/ artifacts; bind-mounted RW
#   /output          -- analysis output (plots, csv, reports); bind-mounted RW
#   /repos           -- parent of read-only git clones; e.g. /repos/console
#
# The pipeline modules live under /app/src; run them as e.g.
#   python -m src.extractor_github ...
ENV PYTHONPATH=/app

# Run as a non-root user. UID/GID are set so the container's writes to
# bind-mounted directories appear with reasonable ownership on the host.
# Override at `docker run` time with --user $(id -u):$(id -g) if needed.
RUN useradd --create-home --uid 1000 analysis
USER analysis

ENTRYPOINT ["python"]
CMD ["-m", "src.extractor_github"]
