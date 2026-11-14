"""女娲的对外请求层：标准库实现，固定超时与有限重试

为什么不用 requests/httpx：项目依赖里没有，且这一点出网不需要额外能力。
所有失败都归成 `degraded_reason`（timeout / network_error / rate_limited /
http_error），调用方据此降级，不把 5xx 抛给前端。
"""
import http.client
import json
import logging
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

USER_AGENT = 'Lasso-Nuwa/1.0 (enterprise agent platform; market sync and knowledge fetch)'

# 固定值，不开放现场配置（见设计文档 5.3）
MARKET_TIMEOUT = 12
SKILL_FILE_TIMEOUT = 10
PAGE_TIMEOUT = 30
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_PAGE_BYTES = 200 * 1024


class RemoteError(Exception):
    """一次出网请求失败，reason 直接作为 degraded_reason 回传"""

    def __init__(self, reason: str, message: str = ''):
        self.reason = reason
        self.message = message or reason
        super().__init__(self.message)


def _reason_of(exc: BaseException, via_proxy: bool = False) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 407:
            return 'proxy_error'
        if exc.code == 429:
            return 'rate_limited'
        return 'http_error'
    if via_proxy:
        # 走代理时连不上/超时/证书错误都归为代理侧问题，便于调用方与审计区分
        return 'proxy_error'
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return 'timeout'
    return 'network_error'


def _ssl_context():
    """显式加载 CA：部分 Windows/精简镜像的 Python 不带系统根证书，
    certifi 存在时优先用它，避免所有 https 都报 CERTIFICATE_VERIFY_FAILED。"""
    import ssl

    context = ssl.create_default_context()
    try:
        import certifi

        context.load_verify_locations(certifi.where())
    except ImportError:
        pass
    return context


def _read_response(response, max_bytes: int) -> tuple:
    raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise RemoteError('too_large', f'响应超过 {max_bytes} 字节上限')
    return raw, dict(response.headers)


def _opener(proxy: str):
    """按代理构造 opener：http 代理同时用于 http/https（CONNECT）

    连接层强制只用 A 记录（见 _ipv4_create_connection），AAAA 不参与真实连接。
    """
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({'http': proxy, 'https': proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler())
    handlers.append(_IPv4HTTPHandler())
    handlers.append(_IPv4HTTPSHandler(context=_ssl_context()))
    return urllib.request.build_opener(*handlers)


def _ipv4_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                            source_address=None, **kwargs):
    """只按 A 记录建连：忽略 AAAA，A 记录为空直接失败

    与 mcp.services.resolved_addresses 的判定口径一致——判定与真实连接都只看 IPv4，
    避免「A 记录公网可连、额外 AAAA 非全局」把整站误拦。
    """
    host, port = address
    infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    if not infos:
        raise OSError(f'{host} 没有可用的 A 记录')
    last_error = None
    for family, socktype, proto, _canon, sockaddr in infos:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            if sock is not None:
                sock.close()
    raise last_error or OSError(f'{host} 的 A 记录无法连接')


class _IPv4HTTPConnection(http.client.HTTPConnection):
    """标准库 HTTPConnection，只把连接目标限制为 IPv4"""

    _create_connection = staticmethod(_ipv4_create_connection)


class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    """标准库 HTTPSConnection，只把连接目标限制为 IPv4；TLS/证书逻辑不变"""

    _create_connection = staticmethod(_ipv4_create_connection)


class _IPv4HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_IPv4HTTPConnection, req)


class _IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_IPv4HTTPSConnection, req, context=self._context)


def http_get(url: str, *, timeout: int = MARKET_TIMEOUT, headers: dict | None = None,
             max_bytes: int = MAX_JSON_BYTES, proxy: str | None = None) -> tuple:
    """GET 一个地址，返回 (bytes, headers)；失败抛 RemoteError

    proxy 只接受 http:// 代理（urllib 原生能力）；走代理时的连接类失败
    统一归为 proxy_error，调用方不得静默改直连。
    """
    request = urllib.request.Request(url, headers={
        'User-Agent': USER_AGENT,
        'Accept': '*/*',
        **(headers or {}),
    })
    try:
        with _opener(proxy).open(request, timeout=timeout) as response:
            return _read_response(response, max_bytes)
    except RemoteError:
        raise
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            socket.timeout, OSError) as exc:
        raise RemoteError(_reason_of(exc, via_proxy=bool(proxy)),
                          f'{type(exc).__name__}: {exc}') from exc


def request_json(url: str, *, timeout: int = MARKET_TIMEOUT, retries: int = 1,
                 backoff: float = 1.0, headers: dict | None = None,
                 proxy: str | None = None) -> dict:
    """GET JSON，失败按 retries 重试（仅网络类错误），最终失败抛 RemoteError"""
    attempt = 0
    while True:
        try:
            raw, _ = http_get(url, timeout=timeout, headers=headers, proxy=proxy)
            return json.loads(raw.decode('utf-8', errors='replace'))
        except RemoteError as exc:
            # 429/4xx 不重试，避免加剧限流
            if attempt >= retries or exc.reason in ('rate_limited', 'http_error', 'too_large'):
                raise
        attempt += 1
        time.sleep(backoff * attempt)


def request_text(url: str, *, timeout: int = SKILL_FILE_TIMEOUT, retries: int = 1,
                 backoff: float = 1.0, headers: dict | None = None,
                 max_bytes: int = MAX_PAGE_BYTES, proxy: str | None = None) -> str:
    attempt = 0
    while True:
        try:
            raw, _ = http_get(url, timeout=timeout, headers=headers,
                              max_bytes=max_bytes, proxy=proxy)
            return raw.decode('utf-8', errors='replace')
        except RemoteError as exc:
            if attempt >= retries or exc.reason in ('rate_limited', 'http_error', 'too_large'):
                raise
        attempt += 1
        time.sleep(backoff * attempt)


def host_of(url: str) -> str:
    return (urlparse(url or '').hostname or '').strip().lower().rstrip('.')


def is_public_https(url: str) -> tuple:
    """女娲只抓公网 https：返回 (是否可用, 原因)"""
    from backend.apps.mcp.services import is_private_host

    parsed = urlparse(url or '')
    if parsed.scheme not in ('http', 'https'):
        return False, '只支持 http/https 地址'
    host = (parsed.hostname or '').strip().lower()
    if not host:
        return False, '地址缺少主机名'
    if is_private_host(host):
        return False, '目标地址属于内网/保留地址段，已按安全策略拒绝'
    if parsed.scheme != 'https':
        return False, '只允许 https 地址'
    return True, ''


def auth_headers(auth_env: str) -> dict:
    """市场源的凭据：只从环境变量取，不落库、不回显"""
    import os

    token = (os.getenv(auth_env, '') if auth_env else '').strip()
    return {'Authorization': f'Bearer {token}'} if token else {}
