# Builds the MCP server only. The `login` command needs a desktop web view and
# is intentionally absent from this image; supply AVEVA_KB_TOKEN instead.
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir .

# Runs over stdio; an MCP client attaches to the container's stdin/stdout.
ENTRYPOINT ["pi-kb-mcp"]
