FROM lfenergy/flexmeasures:v1.1.0rc2

COPY --from=ghcr.io/astral-sh/uv:0.10-python3.12-trixie-slim /usr/local/bin/uv /usr/local/bin/uv

# Plugin-defined automations are not in a FlexMeasures release yet, so the plugin pins
# FlexMeasures to its main branch (see [tool.uv.sources] in pyproject.toml), which takes git to install.
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY . /app/plugins/flexmeasures-openadr3

# Install the plugin dependencies INTO the existing uv environment. The base image's own copy of the
# FlexMeasures source in /app would shadow the FlexMeasures this installs (/app is the working directory),
# so it is removed.
RUN uv pip install --python /app/.venv/bin/python /app/plugins/flexmeasures-openadr3 && \
    rm -rf /app/flexmeasures
