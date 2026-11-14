# SearXNG（天工 L2 默认搜索）

天工网页发现第二层：自建 SearXNG，作为没有 MCP 付费搜索时的默认检索方式，也是 MCP 未配置、
失败或空结果后的回落。只监听回环地址，由 Lasso 从本机调用 `http://127.0.0.1:8888`，
不经过 nginx、不暴露公网。

## 1. 部署形态

| 项 | 值 |
|---|---|
| 源码 | `/opt/searxng`，固定 commit `a7128d5d1d5af2837a68fbeaaee9c3e8391aa9cf` |
| 虚拟环境 | `/opt/searxng/venv`，基于 `/opt/cpython-3.11/bin/python3.11` |
| 配置 | `/etc/searxng/settings.yml`（`searxng:searxng`，0600） |
| 缓存 | `/var/cache/searxng`（`searxng:searxng`） |
| 运行用户 | `searxng:searxng`（系统用户，非 root） |
| 单元 | `/etc/systemd/system/searxng.service` |
| 监听 | `127.0.0.1:8888` |
| 日志 | journald：`journalctl -u searxng` |

部署资产都在 [`deploy/searxng/`](../deploy/searxng/)：`install.sh`（幂等安装）、
`settings.yml`（配置模板）、`searxng.service`（systemd 单元）、`uninstall.sh`（卸载/回滚）。

## 2. 安装

前置：CentOS Stream 8、root 权限、可访问 GitHub，`/opt/cpython-3.11/bin/python3.11` 已就绪。
`pypi.org` 直连超时，脚本默认走清华镜像。

```bash
sudo deploy/searxng/install.sh
```

脚本按顺序做：建系统用户与目录 → `git fetch --depth 1` 固定 commit → 建 venv →
`pip install -r requirements.txt`（镜像源）→ 首次渲染 `settings.yml` → 用 SearXNG 自身加载
校验配置 → 安装单元、`daemon-reload`、`enable --now` → `/healthz` 健康检查（失败打印最近 30 行日志）。

可重复执行：已在该 commit 不重复拉取；`settings.yml` 已存在不覆盖；venv 可用不重建；
pip 已满足不重复下载。可覆盖的环境变量写在脚本头部（`SEARXNG_COMMIT`、`SEARXNG_PYTHON`、
`PIP_INDEX_URL` 等），用法见 `sudo deploy/searxng/install.sh --help`。

## 3. 配置口径

`settings.yml` 模板只保留 5 个引擎，其余全部关闭：

| 引擎 | 出网 | 说明 |
|---|---|---|
| `bing` | 直连 | `base_url: https://cn.bing.com`；`www.bing.com` 在国内会 302，而引擎默认不跟随重定向 |
| `duckduckgo` | 代理 `http://192.0.2.10:7890` | `timeout: 3.0` |
| `wikipedia` | 代理 `http://192.0.2.10:7890` | `timeout: 3.0`；`display_type: ["infobox", "list"]`（只开 infobox 不会进入候选结果） |
| `github` | 直连 | 沿用默认配置 |
| `stackoverflow` | 直连 | 沿用默认配置 |
| `wikidata` | 关闭 | 单次约 3.6 秒只回 1 条，性价比最低；不进 `keep_only` 即不加载 |

其余关键项：`server.bind_address=127.0.0.1`、`server.port=8888`、`limiter: false`、
`search.formats` 含 `json`（不开则 `format=json` 返回 403）、`search.default_lang=zh-CN`、
`outgoing.enable_http=false`（上游只允许 https）。

代理写在引擎级 `proxies`（`all://` 覆盖 http/https），只影响该引擎；`outgoing` 层不设全局代理，
因此其余引擎保持直连。

## 4. 健康检查

```bash
systemctl status searxng
curl -s http://127.0.0.1:8888/healthz                      # 期望 OK
curl -s 'http://127.0.0.1:8888/search?q=ping&format=json' | head -c 200   # 期望 200 + JSON
journalctl -u searxng --since '10 min ago' | grep -ci traceback          # 期望 0
```

老版本没有 `/healthz` 时，用第二条 JSON 检索代替。

## 5. 改配置

线上配置就是 `/etc/searxng/settings.yml`。**代理相关改动走平台**：登录「系统设置 → 网络代理」
维护代理条目与绑定，保存后平台会自动重写该文件并重启服务（流程见第 10 节）。

其余配置（引擎清单、超时、中文默认值等）可直接改文件，然后：

```bash
sudo systemctl restart searxng
```

**不要**为了改配置重跑 `install.sh`——它默认不覆盖已存在的配置。需要按仓库模板整体重置时
（会重新生成 `secret_key`，现有会话失效）才加 `--reset-config`：

```bash
sudo deploy/searxng/install.sh --reset-config
```

注意：平台保存代理绑定时会按仓库模板**全量重写** `settings.yml`，手工改动会被覆盖。
因此引擎清单/超时这类改动应同时回改 `deploy/searxng/settings.yml`，而不是只改线上文件。
模板里 `# __PROXY_<engine>__` 是平台渲染代理的占位符，不能删。

改完先本地校验再重启，可以避免带坏配置拉起：

```bash
sudo -u searxng env SEARXNG_SETTINGS_PATH=/etc/searxng/settings.yml \
  PYTHONPATH=/opt/searxng SEARXNG_CACHE_DIR=/var/cache/searxng \
  /opt/searxng/venv/bin/python -c 'from searx import settings; print("ok")'
```

## 6. 升级

改一个 commit，重跑安装脚本即可（checkout → pip install → 配置校验 → 重启 → 健康检查）：

```bash
sudo SEARXNG_COMMIT=<new-commit> deploy/searxng/install.sh
```

升级前先记下当前 commit（`git -C /opt/searxng rev-parse HEAD`），回滚时填回 `SEARXNG_COMMIT` 重跑。

## 7. 回滚与卸载

只摘服务、保留数据（推荐，便于快速恢复）：

```bash
sudo deploy/searxng/uninstall.sh
```

停止并禁用服务、删除 `/etc/systemd/system/searxng.service` 并 `daemon-reload`；
`/opt/searxng`、`/etc/searxng`、`/var/cache/searxng` 与 `searxng` 用户全部保留。
恢复时重跑 `install.sh`，配置不会被覆盖。

彻底卸载（先把 `settings.yml` 备份到 `/var/backups/searxng-<时间戳>/`，再删目录与用户）：

```bash
sudo deploy/searxng/uninstall.sh --purge --yes
```

Lasso 侧若已接入 L2 开关，同时把它关掉，避免每轮检索先等一次连接超时。

## 8. 平台托管代理（部署步骤）

平台「系统设置 → 网络代理」保存后，会按仓库模板重写 `/etc/searxng/settings.yml` 并重启 SearXNG。
这要求两件服务器侧权限，`install.sh` 已内置（幂等，重跑即可补齐）：

```bash
# 1) 让平台进程（lasso）能写配置，同时保持 searxng:searxng 0600 不变
sudo setfacl -m u:lasso:rw /etc/searxng/settings.yml
# 2) 只放行重启这一个动作；不要给 lasso 完整 sudo
echo 'lasso ALL=(root) NOPASSWD: /usr/bin/systemctl restart searxng' \
  | sudo tee /etc/sudoers.d/lasso-searxng
sudo chmod 440 /etc/sudoers.d/lasso-searxng
sudo visudo -c
# 3) 平台备份目录
sudo mkdir -p /var/backups/lasso-searxng && sudo chown lasso:lasso /var/backups/lasso-searxng
```

`.env` 对应项（默认值即下列内容，通常不用改）：

```dotenv
SEARXNG_SETTINGS_PATH=/etc/searxng/settings.yml
SEARXNG_SRC=/opt/searxng
SEARXNG_VENV_PYTHON=/opt/searxng/venv/bin/python
SEARXNG_HEALTH_URL=http://127.0.0.1:8888/healthz
SEARXNG_RESTART_CMD=sudo -n systemctl restart searxng
SEARXNG_BACKUP_DIR=/var/backups/lasso-searxng
```

**应用链路**：渲染 → `searx` 自身加载校验 → 备份 → 原地覆盖 → `systemctl restart` →
轮询 `/healthz` 10 秒。任一步失败都会恢复备份并再次重启，接口返回 `searxng_apply_failed`，
绑定行标「未生效」；成功则写入审计 `proxy_apply`。备份目录只保留最近 10 份。

**首次把线上文件切到平台模板**：`sudo deploy/searxng/install.sh --reset-config`
（会换 `secret_key`，无业务副作用）。之后不要手工改代理段，保存绑定会整篇重写。

**协议边界**：SearXNG 引擎可用 http/socks5 代理（curl_cffi 支持）；天工正文抓取只接受
http 代理（urllib 原生能力），socks5 条目在天工的下拉里置灰。

## 9. 常见问题

| 症状 | 原因与处理 |
|---|---|
| `Address already in use` | 端口被占用；`ss -ltnp \| grep 8888` 查明后换端口或停掉占用进程 |
| 安装时 pip 长时间卡住 | `pypi.org` 直连超时；确认 `PIP_INDEX_URL` 指向清华/阿里镜像 |
| 重启后 `/healthz` 不通 | `journalctl -u searxng -n 50`；多为配置语法错误或权限问题 |
| JSON 接口 403 | `search.formats` 里没有 `json` |
| 结果明显变少或全空 | 上游引擎限流/改版；先看 journald 里对应引擎的报错，再决定是否临时停用该引擎 |
| 代理引擎超时 | 代理不可达或端口不对；先 `curl -x http://192.0.2.10:7890 -I https://duckduckgo.com` 验证代理本身 |
| `wikipedia` 不出现在结果列表 | 模板已开 `display_type: ["infobox","list"]`；若线上文件被旧模板覆盖，补回该行并重启 |
| 服务反复重启 | `ProtectSystem`/`ReadWritePaths` 与目录权限不匹配；确认 `/var/cache/searxng` 归 `searxng` 所有 |

## 10. 安全边界

- 只监听 `127.0.0.1`，不通过 nginx 暴露，不做公网实例（`public_instance: false`）；
- 运行用户为非登录系统用户，单元带 `NoNewPrivileges`、`PrivateTmp`、`ProtectSystem=full`、`ProtectHome`；
- `enable_http: false`，上游只走 https；`limiter: false` 的前提就是“仅本机调用”；
- 主题词会发给上游搜索引擎，属出网内容；平台只保留候选 URL/标题/摘要，不保存搜索结果页 HTML。
