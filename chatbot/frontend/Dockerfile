# 前端独立镜像（前后端分离部署）：运行 Next.js 自带 Node 服务，
# 通过 next.config.ts 的 rewrites 将 /api/* 代理到 PORTO_API_BASE_URL 指向的后端。
# 构建上下文须为 frontend/ 目录：
#   docker build -t porto-chatbot-frontend -f frontend/Dockerfile frontend
FROM node:20-slim AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci

FROM node:20-slim AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:20-slim AS runner
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./package.json
COPY --from=builder /app/next.config.ts ./next.config.ts

# 运行时通过 -e PORTO_API_BASE_URL=http://backend-host:8100 指向真实后端
ENV PORTO_API_BASE_URL=http://127.0.0.1:8100
EXPOSE 3000

CMD ["npm", "run", "start", "--", "-p", "3000"]
