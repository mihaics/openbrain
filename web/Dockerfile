FROM node:24-alpine AS deps

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

FROM node:24-alpine AS runner

WORKDIR /app
ARG NEXT_PUBLIC_API_BASE=http://localhost:8000
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE

COPY --from=deps /app/node_modules ./node_modules
COPY . .

RUN npm run build

EXPOSE 3777

CMD ["npm", "run", "start"]
