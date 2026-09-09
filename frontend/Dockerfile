FROM node:22-alpine AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY tsconfig.json tsconfig.app.json tsconfig.node.json vite.config.ts vitest.config.ts index.html ./
COPY src ./src
COPY public ./public

ARG VITE_API_BASE=/api/v1
ARG VITE_DEMO_MODE=never
ARG VITE_DEV_LOGIN=false
ARG VITE_APP_NAME=Мехмат.Практикум
ENV VITE_API_BASE=${VITE_API_BASE} \
    VITE_DEMO_MODE=${VITE_DEMO_MODE} \
    VITE_DEV_LOGIN=${VITE_DEV_LOGIN} \
    VITE_APP_NAME=${VITE_APP_NAME}
RUN npm run build

FROM nginx:1.27-alpine AS runtime

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 8080
HEALTHCHECK --interval=20s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1

CMD ["nginx", "-g", "daemon off;"]
