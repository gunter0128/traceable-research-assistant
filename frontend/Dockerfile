# 地端版的網頁（docs/decisions/045）：先用 Node 把前端 build 成靜態檔案，再交給 nginx 提供
# 公開展示版不用這個檔案：前端檔案放在 S3、經過 CloudFront（docs/decisions/013、014）
# nginx 的設定不包在這裡，由 onprem/docker-compose.yml 掛進去

# 第一階段：build。跟 CI 用同一版 Node，Linux 也跟 CI 一樣（Debian 系列）
FROM node:24.22.0-slim AS build
WORKDIR /app
# 先只複製套件清單、裝套件：清單沒變的話，重新 build 時這步會用快取
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# 第二階段：只留 build 出來的檔案，不帶 Node 和 node_modules，image 小很多
FROM nginx:1.30.5-alpine
COPY --from=build /app/dist /usr/share/nginx/html
