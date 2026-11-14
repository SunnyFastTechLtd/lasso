"""平台托管代理：SearXNG 配置渲染/应用，以及天工抓取的代理解析

SearXNG 侧没有 reload 接口，改配置只能 restart，因此「应用」是一条带回滚的链路：
渲染 → 配置校验 → 备份 → 原地覆盖 → restart → /healthz 轮询，任一步失败就恢复上一份。

不引入新依赖：渲染只做占位符文本替换（不引 PyYAML），校验借 SearXNG 自己的 venv
（`python -c 'from searx import settings'`），连通性测试用标准库 urllib；socks5 测试
借 SearXNG venv 里已有的 curl_cffi 子进程。
"""
import json
import logging
import os
import re
import secrets
import shlex
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings
from django.utils import timezone

from backend.apps.settings.models import ProxyEndpoint, ResourceProxyBinding, SystemSetting
from backend.apps.users.models import log_audit

logger = logging.getLogger(__name__)

# SearXNG 侧固定托管的引擎；与 deploy/searxng/settings.yml 的 marker 一一对应
SEARXNG_ENGINES = ['bing', 'duckduckgo', 'wikipedia', 'github', 'stackoverflow']
MARKER = '# __PROXY_{}__'

APPLY_STATE_KEY = 'searxng_proxy_apply_state'
SECRET_RE = re.compile(r'^\s*secret_key:\s*"([^"]+)"', re.MULTILINE)
TEST_TARGET = 'https://www.bing.com/robots.txt'

# 解析结果缓存：每次抓页都查库没必要，30 秒内复用
_CACHE_TTL = 30
_cache_lock = threading.Lock()
_cache: dict = {'at': 0.0, 'items': None}


# ─── 配置路径与运行参数 ──────────────────────────────────────────

def template_path() -> Path:
    return Path(settings.BASE_DIR) / 'deploy' / 'searxng' / 'settings.yml'


def config_path() -> Path:
    return Path(getattr(settings, 'SEARXNG_SETTINGS_PATH', '/etc/searxng/settings.yml'))


def backup_dir() -> Path:
    return Path(getattr(settings, 'SEARXNG_BACKUP_DIR', '/var/backups/lasso-searxng'))


def venv_python() -> str:
    return getattr(settings, 'SEARXNG_VENV_PYTHON', '/opt/searxng/venv/bin/python')


def health_url() -> str:
    return getattr(settings, 'SEARXNG_HEALTH_URL', 'http://127.0.0.1:8888/healthz')


def restart_cmd() -> list:
    return shlex.split(getattr(settings, 'SEARXNG_RESTART_CMD',
                               'sudo -n systemctl restart searxng'))


# ─── 绑定查询 ────────────────────────────────────────────────────

def _active_bindings():
    """启用且代理本身启用的绑定，按资源索引"""
    if _cache['items'] is not None and time.monotonic() - _cache['at'] < _CACHE_TTL:
        return _cache['items']
    rows = (ResourceProxyBinding.objects
            .filter(enabled=True, proxy__enabled=True)
            .select_related('proxy'))
    engines, domains, sources = {}, [], {}
    for row in rows:
        if row.resource_type == ResourceProxyBinding.TYPE_SEARXNG_ENGINE:
            engines[row.resource_key] = row.proxy
        elif row.resource_type == ResourceProxyBinding.TYPE_TIANGONG_DOMAIN:
            domains.append((row.resource_key, row.proxy))
        elif row.resource_type == ResourceProxyBinding.TYPE_TIANGONG_SOURCE:
            sources[row.resource_key] = row.proxy
    items = {'engines': engines, 'domains': domains, 'sources': sources}
    with _cache_lock:
        _cache['items'] = items
        _cache['at'] = time.monotonic()
    return items


def clear_cache():
    with _cache_lock:
        _cache['items'] = None
        _cache['at'] = 0.0


def engine_proxy(name: str):
    return _active_bindings()['engines'].get(name)


def _source_by_id(key: str):
    """来源站绑定依赖 L4 的 TiangongSource；尚未落地时该步自然跳过"""
    try:
        from backend.apps.knowledge.models import TiangongSource
    except ImportError:
        return None
    return TiangongSource.objects.filter(pk=key).first() if str(key).isdigit() else None


def tiangong_proxy_for_url(url: str):
    """解析顺序：精确域名 > 最左通配（最长后缀优先）> 来源站 > 直连

    返回 (proxy_url, reason)。天工只支持 http 代理：urllib 原生 ProxyHandler
    不认 socks5，引入 PySocks 会破坏「不新增依赖」。
    """
    host = (urlparse(url or '').hostname or '').strip().lower().rstrip('.')
    if not host:
        return None, 'no_host'
    items = _active_bindings()

    for key, proxy in items['domains']:
        if key == host:
            return _proxy_url_for_fetch(proxy, 'domain_exact')

    wildcard = None
    for key, proxy in items['domains']:
        if not key.startswith('*.'):
            continue
        base = key[2:]
        if host == base or host.endswith('.' + base):
            if wildcard is None or len(base) > len(wildcard[0]):
                wildcard = (base, proxy)
    if wildcard:
        return _proxy_url_for_fetch(wildcard[1], 'domain_wildcard')

    for key, proxy in items['sources'].items():
        source = _source_by_id(key)
        if source is not None and (getattr(source, 'domain', '') or '').lower() == host:
            return _proxy_url_for_fetch(proxy, 'source')

    return None, 'direct'


def _proxy_url_for_fetch(proxy: ProxyEndpoint, reason: str):
    if proxy.scheme != 'http':
        return None, 'socks5_unsupported'
    return proxy.url(), reason


# ─── SearXNG 配置渲染 ───────────────────────────────────────────

def extract_secret(text: str) -> str:
    """保留线上已有的 secret_key；没有或仍是占位符时新生成一个"""
    match = SECRET_RE.search(text or '')
    if match and match.group(1) and not match.group(1).startswith('__'):
        return match.group(1)
    return secrets.token_hex(32)


def render_settings(existing_text: str = '') -> str:
    """按仓库模板全量渲染：模板 + 引擎代理 + 保留旧 secret_key"""
    path = template_path()
    if not path.is_file():
        raise RuntimeError(f'缺少 SearXNG 配置模板：{path}')
    text = path.read_text(encoding='utf-8')
    text = text.replace('__SEARXNG_SECRET_KEY__', extract_secret(existing_text))

    for name in SEARXNG_ENGINES:
        marker = MARKER.format(name)
        if marker not in text:
            raise RuntimeError(f'模板缺少引擎代理占位符：{marker}')
        proxy = engine_proxy(name)
        if proxy is None:
            text = text.replace('    ' + marker + '\n', '')
        else:
            block = f'    proxies:\n      all://:\n        - {proxy.url()}'
            # marker 前面有 4 个空格缩进，连同缩进一起替换，避免出现 8 空格
            text = text.replace('    ' + marker, block)
    return text


def _write_in_place(path: Path, text: str):
    """原地覆盖而不是 os.replace：保留 searxng:searxng 0600 的属主与权限，
    平台侧只靠 ACL 拿到写权限，无法 chown。"""
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(text)


def _validate(text: str) -> tuple:
    """用 SearXNG 自己的解释器加载一遍配置，返回 (ok, 错误)"""
    python = venv_python()
    if not os.path.exists(python):
        return False, f'找不到 SearXNG 解释器：{python}'
    fd, tmp = tempfile.mkstemp(prefix='searxng-settings-', suffix='.yml')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(text)
        env = os.environ.copy()
        env.update({
            'SEARXNG_SETTINGS_PATH': tmp,
            'PYTHONPATH': getattr(settings, 'SEARXNG_SRC', '/opt/searxng'),
            'SEARXNG_CACHE_DIR': '/tmp',
            'PYTHONDONTWRITEBYTECODE': '1',
        })
        proc = subprocess.run(
            [python, '-c', 'from searx import settings'],
            env=env, capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or '').strip().splitlines()
            return False, (detail[-1] if detail else '配置加载失败')[:300]
        return True, ''
    except subprocess.TimeoutExpired:
        return False, '配置校验超时'
    except Exception as exc:  # noqa: BLE001 - 校验失败统一按不可应用处理
        return False, f'{type(exc).__name__}: {exc}'
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _restart() -> tuple:
    try:
        proc = subprocess.run(restart_cmd(), capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return False, '找不到 sudo/systemctl'
    except subprocess.TimeoutExpired:
        return False, '重启 SearXNG 超时'
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or '').strip().splitlines()
        return False, (detail[-1] if detail else 'restart 失败')[:300]
    return True, ''


def _wait_health(seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health_url(), timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - 服务重启期间连接被拒属预期
            pass
        time.sleep(0.5)
    return False


def _backup(path: Path, text: str) -> Path:
    target_dir = backup_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = target_dir / f'{path.name}.{stamp}'
    backup.write_text(text, encoding='utf-8')
    # 只留最近 10 份，避免长期堆积
    for old in sorted(target_dir.glob(f'{path.name}.*'))[:-10]:
        try:
            old.unlink()
        except OSError:
            pass
    return backup


def save_apply_state(status: str, error: str = '', detail: str = ''):
    SystemSetting.set_value(
        APPLY_STATE_KEY,
        json.dumps({
            'status': status,
            'error': error,
            'detail': detail,
            'at': timezone.now().isoformat(),
        }, ensure_ascii=False),
        'SearXNG 代理配置最近一次应用结果',
    )


def load_apply_state() -> dict:
    raw = SystemSetting.get_value(APPLY_STATE_KEY, '')
    try:
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def _mark_applied(flag: bool):
    # 只有 SearXNG 引擎绑定需要“应用”才生效；天工绑定是运行时解析，保存即生效
    ResourceProxyBinding.objects.filter(
        resource_type=ResourceProxyBinding.TYPE_SEARXNG_ENGINE).update(applied=flag)


def apply_searxng(actor=None) -> dict:
    """把当前绑定写进线上 settings.yml；失败自动回滚到上一份并重启"""
    path = config_path()
    if not path.is_file():
        save_apply_state('failed', 'searxng_config_missing')
        _mark_applied(False)
        return {'applied': False, 'error': 'searxng_config_missing',
                'detail': f'找不到 {path}，请先按 docs/SEARXNG.md 部署 SearXNG'}

    try:
        old_text = path.read_text(encoding='utf-8')
        new_text = render_settings(old_text)
    except Exception as exc:  # noqa: BLE001
        clear_cache()
        save_apply_state('failed', 'searxng_config_render_failed', str(exc))
        _mark_applied(False)
        return {'applied': False, 'error': 'searxng_config_render_failed', 'detail': str(exc)}

    if new_text == old_text:
        _mark_applied(True)
        save_apply_state('ok', '', '配置无变化')
        return {'applied': True, 'changed': False, 'detail': '配置无变化'}

    ok, detail = _validate(new_text)
    if not ok:
        save_apply_state('failed', 'searxng_config_invalid', detail)
        _mark_applied(False)
        log_audit(actor=actor, action='proxy_apply', target='SearXNG 代理配置',
                  detail=f'失败（配置校验未通过）：{detail[:200]}')
        return {'applied': False, 'error': 'searxng_config_invalid', 'detail': detail}

    try:
        backup = _backup(path, old_text)
    except OSError as exc:
        save_apply_state('failed', 'searxng_backup_failed', str(exc))
        _mark_applied(False)
        return {'applied': False, 'error': 'searxng_backup_failed', 'detail': str(exc)}

    try:
        _write_in_place(path, new_text)
    except OSError as exc:
        save_apply_state('failed', 'searxng_write_denied', str(exc))
        _mark_applied(False)
        return {'applied': False, 'error': 'searxng_write_denied', 'detail': str(exc)}

    restarted, restart_error = _restart()
    healthy = restarted and _wait_health(10)
    if healthy:
        _mark_applied(True)
        clear_cache()
        save_apply_state('ok', '', f'备份：{backup.name}')
        detail = _bindings_summary()
        log_audit(actor=actor, action='proxy_apply', target='SearXNG 代理配置',
                  detail=f'已应用并重启通过健康检查；{detail}')
        return {'applied': True, 'changed': True, 'detail': f'已应用；{detail}'}

    # 回滚：恢复上一份配置并再重启一次；健康检查只做尽力而为，不掩盖原始错误
    error = 'searxng_restart_failed' if not restarted else 'searxng_health_failed'
    message = restart_error if not restarted else '/healthz 10 秒内未恢复'
    rollback_ok = False
    try:
        _write_in_place(path, old_text)
        rolled, _ = _restart()
        rollback_ok = rolled and _wait_health(10)
    except OSError as exc:
        message = f'{message}；回滚失败：{exc}'

    _mark_applied(False)
    clear_cache()
    save_apply_state('failed', error, message)
    log_audit(actor=actor, action='proxy_apply', target='SearXNG 代理配置',
              detail=f'失败：{error}（{message[:150]}），已回滚备份 {backup.name}'
                     f'{"，服务已恢复" if rollback_ok else "，服务可能未恢复"}')
    return {'applied': False, 'error': error,
            'detail': f'{message}；已回滚到上一份配置'}


def _bindings_summary() -> str:
    parts = []
    for name in SEARXNG_ENGINES:
        proxy = engine_proxy(name)
        parts.append(f'{name}={proxy.name if proxy else "直连"}')
    return '，'.join(parts)


# ─── 代理连通性测试 ─────────────────────────────────────────────

def _reason_of(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return 'auth_failed' if exc.code == 407 else 'http_error'
    if isinstance(exc, (TimeoutError, OSError)) and 'timed out' in str(exc).lower():
        return 'timeout'
    if isinstance(exc, ConnectionRefusedError):
        return 'refused'
    text = str(exc).lower()
    if 'certificate' in text or 'ssl' in text:
        return 'tls_error'
    if 'timed out' in text or 'timeout' in text:
        return 'timeout'
    if 'refused' in text:
        return 'refused'
    return 'network_error'


def _test_http(proxy: ProxyEndpoint) -> tuple:
    url = proxy.url()
    handlers = [
        urllib.request.ProxyHandler({'http': url, 'https': url}),
        urllib.request.HTTPSHandler(context=_ssl_context()),
    ]
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(TEST_TARGET, headers={'User-Agent': 'Lasso-ProxyTest/1.0'})
    with opener.open(request, timeout=10) as resp:
        resp.read(1024)
        return True, f'HTTP {resp.status}'


def _test_socks5(proxy: ProxyEndpoint) -> tuple:
    """借 SearXNG venv 里已有的 curl_cffi，不新增 PySocks 依赖"""
    python = venv_python()
    if not os.path.exists(python):
        return False, f'找不到 {python}，socks5 无法本地测试'
    code = (
        'import sys\n'
        'from curl_cffi import requests\n'
        'r = requests.get(sys.argv[1], proxies={"http": sys.argv[2], "https": sys.argv[2]},'
        ' timeout=10, impersonate="chrome")\n'
        'print(r.status_code)\n'
    )
    try:
        proc = subprocess.run([python, '-c', code, TEST_TARGET, proxy.url()],
                              capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return False, '通过 socks5 访问目标超时'
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or '').strip().splitlines()
        return False, (detail[-1] if detail else 'socks5 连接失败')[:300]
    return True, f'HTTP {(proc.stdout or "").strip()}'


def _ssl_context():
    import ssl

    context = ssl.create_default_context()
    try:
        import certifi

        context.load_verify_locations(certifi.where())
    except ImportError:
        pass
    return context


def test_endpoint(proxy: ProxyEndpoint, actor=None) -> dict:
    """测试并落库最近结果；失败只记录分类与原文尾部，不含密码"""
    started = time.monotonic()
    try:
        if proxy.scheme == 'socks5':
            ok, message = _test_socks5(proxy)
        else:
            ok, message = _test_http(proxy)
        reason = '' if ok else message
    except Exception as exc:  # noqa: BLE001 - 分类后回传，不抛给前端
        ok = False
        reason = _reason_of(exc)
        message = f'{type(exc).__name__}: {exc}'

    elapsed = int((time.monotonic() - started) * 1000)
    proxy.last_test_at = timezone.now()
    proxy.last_test_status = 'ok' if ok else 'failed'
    proxy.last_test_ms = elapsed
    proxy.last_test_error = '' if ok else (message or reason)[:500]
    proxy.save(update_fields=['last_test_at', 'last_test_status', 'last_test_ms',
                              'last_test_error', 'updated_at'])
    log_audit(actor=actor, action='proxy_test', target=proxy.name,
              detail=f'{"通过" if ok else "失败"}，{elapsed}ms'
                     f'{"" if ok else f"，原因：{reason}"}')
    return {'ok': ok, 'status': 'ok' if ok else 'failed', 'elapsed_ms': elapsed,
            'reason': reason, 'message': message}


# ─── 界面状态 ────────────────────────────────────────────────────

def binding_state() -> dict:
    from backend.apps.settings.serializers import ProxyEndpointSerializer

    rows = ResourceProxyBinding.objects.select_related('proxy').all()
    engines = {name: None for name in SEARXNG_ENGINES}
    tiangong = []
    for row in rows:
        if row.resource_type == ResourceProxyBinding.TYPE_SEARXNG_ENGINE:
            if row.resource_key in engines:
                engines[row.resource_key] = row.proxy_id
        elif row.resource_type == ResourceProxyBinding.TYPE_TIANGONG_DOMAIN:
            tiangong.append({'kind': 'domain', 'key': row.resource_key,
                             'proxy_id': row.proxy_id, 'applied': row.applied})
        elif row.resource_type == ResourceProxyBinding.TYPE_TIANGONG_SOURCE:
            tiangong.append({'kind': 'source', 'key': row.resource_key,
                             'proxy_id': row.proxy_id, 'applied': row.applied})

    configured = {row.resource_key: row.applied for row in rows}
    return {
        'proxies': ProxyEndpointSerializer(ProxyEndpoint.objects.all(), many=True).data,
        'engines': [{'name': name, 'proxy_id': engines[name],
                     'applied': configured.get(name, False)} for name in SEARXNG_ENGINES],
        'tiangong': tiangong,
        'sources': _available_sources(),
        'apply': load_apply_state(),
    }


def _available_sources() -> list:
    try:
        from backend.apps.knowledge.models import TiangongSource
    except ImportError:
        return []
    return list(TiangongSource.objects.values('id', 'name', 'domain')[:200])
