# systemd 单元说明

`lasso.service` 用 gunicorn 托管 Django WSGI，部署根目录按 `/srv/lasso` 编写，
换成自己的目录时同步修改 `WorkingDirectory` 与 `ExecStart`。

## 安装

```bash
sudo cp lasso.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lasso
sudo systemctl status lasso
```

单元里不写 `EnvironmentFile`：`settings.py` 用 python-dotenv 读取
`WorkingDirectory` 下的 `.env`，因此 `WorkingDirectory` 必须指向仓库根目录。

## Celery worker

耗时任务（知识库切片/OCR、天工采集）通过 Celery 执行。没有 worker 时这些任务会在
Web 请求内同步跑完，接口会明显变慢；生产环境应同时启用 Web 与 worker 两个单元。

```bash
sudo cp lasso-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lasso-worker
sudo systemctl status lasso-worker
```

前置条件：可访问的 Redis，并在 `.env` 里设置

```dotenv
CELERY_TASK_ALWAYS_EAGER=False
CELERY_BROKER_URL=redis://127.0.0.1:6379/1
CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/2
```

改完 `.env` 需要重启 `lasso`（Web 进程读取 eager 开关）与 `lasso-worker`。

## 知识库 worker（knowledge 队列）

文档切片、OCR 入库与天工采集统一走 `knowledge` 队列，由
`lasso-knowledge-worker.service` 消费；`lasso-worker.service` 只消费默认队列，
其队列与并发保持不动，两者可以并存。天工任务显式声明 `queue='knowledge'`，
避免落到默认队列被旧版 worker 用旧代码执行。

```bash
sudo cp lasso-knowledge-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lasso-knowledge-worker
sudo systemctl status lasso-knowledge-worker
```

并发按 OCR 引擎选择：

- **多模态模型 OCR（默认）**：`Environment=KNOWLEDGE_CONCURRENCY=2`，单页主要是等待
  模型返回，2 个并发足够且不会互相拖慢。
- **本机 tesseract OCR**：改成 `KNOWLEDGE_CONCURRENCY=1`，tesseract 会吃满 CPU，
  并行只会让每页都变慢。

`acks_late` 与 `soft_time_limit=900`/`time_limit=960` 写在
`backend/apps/knowledge/tasks.py` 的任务装饰器里；单元只负责进程托管，并用
`--prefetch-multiplier=1` 避免一次预取多份大文档。停机时 `TimeoutStopSec=120`
给正在收尾的任务留时间，超时未 ack 的任务会由 Redis 重新投递。

没有知识库 worker 时，任务仍会停留在 `knowledge` 队列（不会被 `lasso-worker`
消费），文档状态停在 `pending`，直到该单元启动。

### OCR 前置：tesseract 与中文语言包

多模态模型不可用时才降级到本机 tesseract；未安装不会阻塞入库，但相关页会记为
未解析。部署时执行：

```bash
deploy/ops/install_ocr.sh --check     # 只检查
sudo deploy/ops/install_ocr.sh --yes  # 安装 tesseract + chi_sim + eng
```

体积约 60–90 MB（含依赖），校验方式是 `tesseract --list-langs` 同时包含
`chi_sim` 与 `eng`。缺装时的降级行为见脚本头部注释与
[../../docs/DEPLOYMENT.md](../../docs/DEPLOYMENT.md)。

## 运行参数取舍

- `--workers 1 --worker-class gthread --threads 8`：SSE 流式对话会长期占用线程，
  用线程承接并发流；登录失败节流默认走进程内缓存，多 worker 会把阈值放大成 N 倍，
  扩容前需要先把缓存换成共享后端（如 Redis）。
- `--timeout 600`：单次对话可能包含多次模型调用，超时设短会在长回答时被切断。
- `Restart=always` + `RestartSec=3`：进程异常退出后自动拉起；`TimeoutStopSec=45`
  给正在写的会话留出收尾时间。
- `PrivateTmp`：最小化临时目录暴露。
- `lasso.service` **不启用** `NoNewPrivileges`：平台应用 SearXNG 代理配置时通过
  `sudo systemctl restart searxng` 重启服务，该选项会禁用 sudo 的 setuid 提权，
  开启后代理应用会以 `searxng_restart_failed`（有效用户 ID 不是 0）失败。
  `lasso-worker.service` 不涉及提权，保留 `NoNewPrivileges=true` 这项加固。

## 日志与排障

```bash
journalctl -u lasso -f
journalctl -u lasso --since '10 min ago' | grep -c Traceback
```

完整部署与回滚步骤见 [../../docs/DEPLOYMENT.md](../../docs/DEPLOYMENT.md)。
