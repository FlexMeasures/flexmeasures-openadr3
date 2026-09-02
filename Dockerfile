FROM lfenergy/flexmeasures:v1.0.0
 
COPY --from=ghcr.io/astral-sh/uv:0.10-python3.12-trixie-slim /usr/local/bin/uv /usr/local/bin/uv

COPY . /app/plugins/flexmeasures-openadr3

# Install the plugin dependencies INTO the existing uv environment
RUN uv pip install --python /app/.venv/bin/python /app/plugins/flexmeasures-openadr3