FROM oven/bun:1.4.2

WORKDIR /app
ENV NODE_ENV=production

COPY package.json ./
RUN bun install --production

COPY src ./src

EXPOSE 8080
CMD ["bun", "run", "src/server.js"]
