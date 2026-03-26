# Node.js 20 + Python 3 slim base
FROM node:20-slim

# Install Python, pip, and Playwright system deps
RUN apt-get update && apt-get install -y \
    python3 python3-pip \
    firefox-esr \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    && rm -rf /var/lib/apt/lists/*

# Install Playwright MCP
RUN npm install -g @playwright/mcp@latest

# Install Playwright firefox browser via npx
RUN npx playwright install firefox

WORKDIR /app

# Python dependencies
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt --break-system-packages

# App files
COPY . .
COPY playwright-mcp-config.json /config.json
COPY start-render.sh /start-render.sh
RUN chmod +x /start-render.sh

ENTRYPOINT []
CMD ["/start-render.sh"]
