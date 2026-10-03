FROM oven/bun:1.4.2

WORKDIR /app
ENV NODE_ENV=production

# Zero npm dependencies — bun runs the source directly, no install, no build.
COPY package.json ./
COPY src ./src

EXPOSE 8080
CMD ["bun", "run", "src/server.js"]
