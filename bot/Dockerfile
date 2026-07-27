# bot/Dockerfile
FROM node:20-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY package*.json ./
RUN npm ci --only=production

# Copy source
COPY src/ ./src/

# auth_info/ is mounted as a volume at runtime — do not copy it in

CMD ["node", "src/index.js"]