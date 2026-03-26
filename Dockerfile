FROM mcr.microsoft.com/playwright/mcp

COPY playwright-mcp-config.json /config.json

ENTRYPOINT []
CMD ["sh", "-c", "node cli.js --config /config.json --browser firefox --headless --port ${PORT:-8931} --host 0.0.0.0 --image-responses omit"]
