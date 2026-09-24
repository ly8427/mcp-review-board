# mcp-review-board — container for Glama listing checks (and any containerized
# deployment). The server is a Streamable-HTTP MCP server: it must bind
# 0.0.0.0 inside the container to be reachable from outside.
FROM python:3.12-slim

RUN pip install --no-cache-dir mcp-review-board==2.4.1

ENV REVIEWBOARD_HOST=0.0.0.0 \
    REVIEWBOARD_PORT=8765

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/')" || exit 1

CMD ["review-board"]
