"""
Django settings for backend project.
"""
import os
from pathlib import Path
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('SECRET_KEY', 'django-insecure-default-key')
DEBUG = os.getenv('DEBUG', 'True') == 'True'

ALLOWED_HOSTS = [h for h in os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if h]

# 是否开放自助注册接口（企业内网场景默认关闭，由管理员建号）
ALLOW_REGISTER = os.getenv('ALLOW_REGISTER', 'False') == 'True'

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third-party
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',

    # Local apps —— Lasso 底座：用户/知识库/对话/系统设置/智能体内核/公共
    'backend.apps.users',
    'backend.apps.knowledge',
    'backend.apps.chat',
    'backend.apps.settings',
    'backend.apps.agents',
    # Lasso 平台装配三域：智能体定义 / MCP 工具来源 / 技能规范
    'backend.apps.agenthub',
    'backend.apps.mcp',
    'backend.apps.skills',
    # F4 用量概览（只读聚合）
    'backend.apps.usage',
    # 女娲模式（自然语言建智能体 + 市场目录 + 资料抓取）
    'backend.apps.nuwa',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'corsheaders.middleware.CorsMiddleware',  # 必须在 CommonMiddleware 之前
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    # /media/ 鉴权 Cookie：登录态靠 HttpOnly Cookie 传给 nginx auth_request
    'backend.apps.common.media_auth.MediaTokenCookieMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'backend.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'backend.wsgi.application'

# Database
db_engine = os.getenv('DB_ENGINE', 'sqlite3')
if db_engine == 'sqlite3':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / os.getenv('DB_NAME', 'db.sqlite3'),
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.getenv('DB_NAME', ''),
            'USER': os.getenv('DB_USER', ''),
            'PASSWORD': os.getenv('DB_PASSWORD', ''),
            'HOST': os.getenv('DB_HOST', '127.0.0.1'),
            'PORT': os.getenv('DB_PORT', '5432'),
        }
    }

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# Custom user model
AUTH_USER_MODEL = 'users.User'

# Internationalization
LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Media files (uploads)
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# DRF 配置
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    # 标准分页：认 page_size 查询参数（前端列表统一传 page_size=100）；
    # 用 DRF 默认 PageNumberPagination 时 page_size 被忽略，列表超 20 条被静默截断
    'DEFAULT_PAGINATION_CLASS': 'backend.apps.common.pagination.StandardPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_RENDERER_CLASSES': (
        'rest_framework.renderers.JSONRenderer',
    ),
    'EXCEPTION_HANDLER': 'backend.apps.common.exceptions.custom_exception_handler',
}

# Simple JWT 配置
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=int(os.getenv('JWT_ACCESS_TOKEN_LIFETIME_MINUTES', 60))),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=int(os.getenv('JWT_REFRESH_TOKEN_LIFETIME_DAYS', 7))),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': os.getenv('JWT_SIGNING_KEY', SECRET_KEY),
    'AUTH_HEADER_TYPES': ('Bearer',),
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
}

# 登录失败节流：同一「用户名（忽略大小写）+ 客户端 IP」在窗口内失败达到阈值即锁定。
# 阈值 <= 0 表示关闭节流；锁定期内即使口令正确也拒绝，避免被用来探测口令是否已改对。
# 计数存于 Django cache：默认 LocMemCache 仅单进程有效，多 worker 部署必须配置
# 共享缓存（Redis/Memcached），否则各 worker 独立计数，实际阈值会被放大。
LOGIN_FAILURE_LIMIT = int(os.getenv('LOGIN_FAILURE_LIMIT', 5))
LOGIN_FAILURE_WINDOW_SECONDS = int(os.getenv('LOGIN_FAILURE_WINDOW_SECONDS', 300))

# 可信反向代理清单（IP 或 CIDR，逗号分隔）。默认空 = 不信任任何代理，
# 完全忽略 X-Forwarded-For，客户端伪造该头无效。
# 生产走 nginx 时，把 nginx 到 Django 的出口地址（如 127.0.0.1 或网段 CIDR）填进来，
# 且 nginx 必须用 $proxy_add_x_forwarded_for 追加而非覆盖。
AUTH_TRUSTED_PROXIES = [
    p.strip() for p in os.getenv('AUTH_TRUSTED_PROXIES', '').split(',') if p.strip()
]

# CORS 配置
CORS_ALLOW_ALL_ORIGINS = DEBUG  # 开发环境允许所有来源
CORS_ALLOW_CREDENTIALS = True
if not DEBUG:
    # 生产环境必须从 .env 注入白名单，默认仅保留本地构建预览地址
    _cors_origins = os.getenv('CORS_ALLOWED_ORIGINS', 'http://localhost:5173,http://127.0.0.1:5173')
    CORS_ALLOWED_ORIGINS = [o for o in _cors_origins.split(',') if o]
    CSRF_TRUSTED_ORIGINS = CORS_ALLOWED_ORIGINS.copy()

# 上传文件大小限制
FILE_UPLOAD_MAX_MEMORY_SIZE = int(os.getenv('UPLOAD_MAX_SIZE', 100 * 1024 * 1024))
DATA_UPLOAD_MAX_MEMORY_SIZE = int(os.getenv('UPLOAD_MAX_SIZE', 100 * 1024 * 1024))

# 大模型配置：唯一来源是数据库「系统设置 → 模型接入与凭据」。
# 不再从环境变量读取 LLM_*，也不提供 env 兜底（缺配置时接口会明确报错）。

# 向量数据库配置
QDRANT_HOST = os.getenv('QDRANT_HOST', '127.0.0.1')
QDRANT_PORT = int(os.getenv('QDRANT_PORT', 6333))
QDRANT_COLLECTION_PREFIX = os.getenv('QDRANT_COLLECTION_PREFIX', 'lasso')

# 向量检索后端：local=DocumentChunk 内存余弦（默认，零依赖）；qdrant=Qdrant ANN 检索
VECTOR_STORE = os.getenv('VECTOR_STORE', 'local')

# 嵌入模型：本地 fastembed（纯离线，无 API 配置项）
FASTEMBED_MODEL = os.getenv('FASTEMBED_MODEL', 'BAAI/bge-small-zh-v1.5')
FASTEMBED_CACHE_DIR = os.getenv('FASTEMBED_CACHE_DIR', str(BASE_DIR / 'models' / 'fastembed'))

# Celery 异步任务（Redis 作 broker）
# 默认 eager 同步执行，便于本地开发/测试（无 Redis 也能跑）；
# 生产环境 .env 必须显式 CELERY_TASK_ALWAYS_EAGER=False 走真正的异步队列。
CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', 'redis://127.0.0.1:6379/1')
CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', 'redis://127.0.0.1:6379/2')
CELERY_TASK_ALWAYS_EAGER = os.getenv('CELERY_TASK_ALWAYS_EAGER', 'True') == 'True'
CELERY_TASK_TIME_LIMIT = int(os.getenv('CELERY_TASK_TIME_LIMIT', 6 * 3600))  # 大文档向量化可能很久
CELERY_WORKER_HIJACK_ROOT_LOGGER = False

# 知识库单独一个队列：文档 OCR/向量化是重任务，不和对话/智能体抢 worker。
# knowledge 队列承载文档处理与天工采集；lasso-worker 只消费默认 celery 队列。
CELERY_TASK_DEFAULT_QUEUE = os.getenv('CELERY_TASK_DEFAULT_QUEUE', 'celery')
CELERY_TASK_ROUTES = {
    'backend.apps.knowledge.tasks.process_document_task': {'queue': 'knowledge'},
    'backend.apps.knowledge.tasks.tiangong_run_task': {'queue': 'knowledge'},
}

# 知识库 OCR（P0 扫描 PDF / P1 图片）：auto = 多模态模型 → tesseract → 明确失败。
# 对话临时附件不会走这条链路（services.extract_text 不做 OCR）。
KNOWLEDGE_OCR_MIN_TEXT_CHARS = int(os.getenv('KNOWLEDGE_OCR_MIN_TEXT_CHARS', '16'))
KNOWLEDGE_OCR_MAX_PAGES = int(os.getenv('KNOWLEDGE_OCR_MAX_PAGES', '200'))
KNOWLEDGE_OCR_RENDER_SCALE = float(os.getenv('KNOWLEDGE_OCR_RENDER_SCALE', '2.0'))
KNOWLEDGE_OCR_LANG = os.getenv('KNOWLEDGE_OCR_LANG', 'chi_sim+eng')
KNOWLEDGE_OCR_TIMEOUT = int(os.getenv('KNOWLEDGE_OCR_TIMEOUT', '60'))
KNOWLEDGE_OCR_LLM_TIMEOUT = int(os.getenv('KNOWLEDGE_OCR_LLM_TIMEOUT', '60'))
KNOWLEDGE_OCR_LLM_MAX_TOKENS = int(os.getenv('KNOWLEDGE_OCR_LLM_MAX_TOKENS', '4096'))

# ─── Lasso 平台装配 ───

# MCP 密钥落库加密（PRD B5）：Fernet key，缺失时密钥字段拒绝写入明文
MCP_SECRET_KEY = os.getenv('MCP_SECRET_KEY', '')

# SearXNG（天工 L2 默认搜索）平台托管：代理配置由平台改写该文件并重启服务。
# 部署前置（settings.yml 的 ACL、sudoers 只放行 restart）见 docs/SEARXNG.md。
SEARXNG_SETTINGS_PATH = os.getenv('SEARXNG_SETTINGS_PATH', '/etc/searxng/settings.yml')
SEARXNG_SRC = os.getenv('SEARXNG_SRC', '/opt/searxng')
SEARXNG_VENV_PYTHON = os.getenv('SEARXNG_VENV_PYTHON', '/opt/searxng/venv/bin/python')
SEARXNG_HEALTH_URL = os.getenv('SEARXNG_HEALTH_URL', 'http://127.0.0.1:8888/healthz')
SEARXNG_RESTART_CMD = os.getenv('SEARXNG_RESTART_CMD', 'sudo -n systemctl restart searxng')
SEARXNG_BACKUP_DIR = os.getenv('SEARXNG_BACKUP_DIR', '/var/backups/lasso-searxng')

# 技能 DB → 磁盘镜像根目录，LocalSkillLoader 从此目录扫描（可行性报告 4.2）
SKILLS_MIRROR_DIR = Path(os.getenv('SKILLS_MIRROR_DIR', str(MEDIA_ROOT / 'skills')))
