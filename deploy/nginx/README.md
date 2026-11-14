# nginx 配置样例

本目录是 Lasso 线上 nginx 配置的通用样例，路径统一以 `/srv/lasso` 为部署根目录；
换成自己的目录时，同步改 `nginx.conf` 的 `root` 与 `lasso-media.conf` 的 `alias`。

| 文件 | 建议安装位置 | 作用 |
|---|---|---|
| `nginx.conf` | `/etc/nginx/nginx.conf` | 主配置：80 端口 `default_server`，SPA 静态根指向 `frontend/dist` |
| `lasso-api.conf` | `/etc/nginx/default.d/lasso-api.conf` | `/api/` 反代到 `127.0.0.1:8000`，关闭缓冲（SSE 必需） |
| `lasso-media.conf` | `/etc/nginx/default.d/lasso-media.conf` | `/media/` 先经 `auth_request` 鉴权，再由 nginx 直出文件 |

## 安装与生效

```bash
sudo cp nginx.conf /etc/nginx/nginx.conf
sudo cp lasso-api.conf lasso-media.conf /etc/nginx/default.d/
sudo nginx -t && sudo systemctl reload nginx
```

`default.d/*.conf` 由主配置在 `server { }` 内 `include`，所以这两个片段不能再写
`server {}` 或 `listen`。

## 几个不能省的细节

- **SSE 必须关缓冲**：`lasso-api.conf` 里的 `proxy_buffering off` 与较长的
  `proxy_read_timeout` 是流式对话能逐字返回的前提，删掉会变成一次性返回。
- **媒体鉴权走子请求**：`/media/` 不直接 `alias` 暴露 `MEDIA_ROOT`，而是先请求
  `/api/media/auth/` 校验所有权与知识库可见范围；`branding/` 是唯一公开例外。
- **穿越拦截在原始 URI 上做**：nginx 会先归一化 URI 再匹配 `location`，所以
  `lasso-media.conf` 顶部用 `$request_uri` 正则拦截 `..` 与 `%2e%2e`。
- **SPA 回退**：`try_files $uri $uri/ /index.html` 让前端路由刷新不 404。
- 反向代理后，后端拿到的客户端 IP 需要 `X-Forwarded-For`；如启用登录失败节流或
  审计来源，把代理地址写进 `.env` 的 `AUTH_TRUSTED_PROXIES`。

完整部署步骤见 [../../docs/DEPLOYMENT.md](../../docs/DEPLOYMENT.md)。
