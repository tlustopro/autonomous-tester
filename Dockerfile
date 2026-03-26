FROM mcr.microsoft.com/playwright/mcp

# Install Python
RUN apt-get update && apt-get install -y python3 python3-pip && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt --break-system-packages

COPY . .
COPY playwright-mcp-config.json /config.json

COPY start-render.sh /start-render.sh
RUN chmod +x /start-render.sh

ENTRYPOINT []
CMD ["/start-render.sh"]
