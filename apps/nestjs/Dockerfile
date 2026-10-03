# --- build stage: compile TypeScript ---
FROM node:22-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json ./
COPY src ./src
RUN ./node_modules/.bin/tsc -p tsconfig.json

# --- runtime stage: no compilers ---
FROM node:22-slim
WORKDIR /app
ENV NODE_ENV=production
COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY --from=build /app/dist ./dist

EXPOSE 8080
CMD ["node", "dist/main.js"]
