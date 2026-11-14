"""出厂内置演示工具来源（PRD B7）——「演示设备状态查询」

作用：让 P0 押轴旅程不依赖任何客户系统即可验收（PRD 5.1）。
形态：接口型（streamable HTTP）MCP 服务，默认监听 http://127.0.0.1:8931/mcp 。

三个工具：
- list_offline_devices  查询离线设备列表（只读，业务侧可见）
- device_detail         查询单台设备详情（只读，业务侧可见）
- update_device_status  改状态（写操作，仅管理端可见）

「仅管理端可见」的实现口径：请求头带 X-Lasso-Demo-Admin 且值与
LASSO_DEMO_ADMIN_TOKEN 一致时，才在 tools/list 中披露 update_device_status，
调用时才放行。平台侧注册内置来源时**不写**这个头，因此业务用户装配出来的
会话永远看不到、也调不动写工具；管理端验收时单独带上该头即可改数据。

设备状态持久化在 data/demo_devices.json，首次运行自动落一份出厂数据，
验收时改的状态会保留（这正是 PRD 完成证据要的"改动后的实时数据"）。

启动（不依赖 Django，独立进程）：
    python -m backend.apps.mcp.demo_server
"""
import json
import os
import threading
from pathlib import Path

from mcp.server.fastmcp import FastMCP

SERVER_NAME = '演示设备状态查询'
DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8931
DEFAULT_HTTP_PATH = '/mcp'
ADMIN_HEADER = 'x-lasso-demo-admin'

DATA_FILE = Path(os.environ.get(
    'LASSO_DEMO_DATA',
    Path(__file__).resolve().parents[3] / 'data' / 'demo_devices.json'))

_WRITE_TOOL = 'update_device_status'
_ALLOWED_STATUS = ('online', 'offline')
_LOCK = threading.Lock()

# 出厂演示数据：覆盖两个部门、在线/离线混合，够跑通"改状态→再提问"的验收动作
_SEED_DEVICES = [
    {'device_id': 'LTV-0231', 'name': '注塑机 3 号', 'dept': '设备运维部',
     'location': '一号车间 A 区', 'status': 'offline', 'firmware': '2.4.1',
     'last_heartbeat': '2026-10-21 07:42:15', 'fault_code': 'E12 液压压力低'},
    {'device_id': 'LTV-0288', 'name': '空压机 1 号', 'dept': '设备运维部',
     'location': '动力站', 'status': 'offline', 'firmware': '1.9.7',
     'last_heartbeat': '2026-10-21 06:18:03', 'fault_code': ''},
    {'device_id': 'LTV-0305', 'name': '贴片机 2 号', 'dept': '设备运维部',
     'location': '二号车间 B 区', 'status': 'online', 'firmware': '3.1.0',
     'last_heartbeat': '2026-10-21 09:05:44', 'fault_code': ''},
    {'device_id': 'PDA-1102', 'name': '巡检终端 12 号', 'dept': '一线班组',
     'location': '成品仓', 'status': 'offline', 'firmware': '5.0.2',
     'last_heartbeat': '2026-10-20 18:33:09', 'fault_code': '电量耗尽'},
    {'device_id': 'PDA-1130', 'name': '巡检终端 15 号', 'dept': '一线班组',
     'location': '一号车间', 'status': 'online', 'firmware': '5.0.2',
     'last_heartbeat': '2026-10-21 09:02:51', 'fault_code': ''},
    {'device_id': 'EMS-0417', 'name': '电能表 4 号', 'dept': '一线班组',
     'location': '配电房', 'status': 'offline', 'firmware': '1.2.0',
     'last_heartbeat': '2026-10-19 11:27:36', 'fault_code': '通信模块无响应'},
]


def _load() -> list:
    """读设备台账；文件不存在时落一份出厂数据"""
    with _LOCK:
        if not DATA_FILE.exists():
            DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
            DATA_FILE.write_text(
                json.dumps(_SEED_DEVICES, ensure_ascii=False, indent=2), 'utf-8')
        return json.loads(DATA_FILE.read_text('utf-8'))


def _save(devices: list) -> None:
    with _LOCK:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        DATA_FILE.write_text(
            json.dumps(devices, ensure_ascii=False, indent=2), 'utf-8')


mcp = FastMCP(
    SERVER_NAME,
    host=os.environ.get('LASSO_DEMO_HOST', DEFAULT_HOST),
    port=int(os.environ.get('LASSO_DEMO_PORT', DEFAULT_PORT)),
    streamable_http_path=os.environ.get('LASSO_DEMO_PATH', DEFAULT_HTTP_PATH),
    stateless_http=True,
)


@mcp.tool(description='查询离线设备列表，可按部门名称过滤；返回设备编号、名称、位置、最后心跳与故障码')
async def list_offline_devices(dept: str = '') -> dict:
    devices = [d for d in _load() if d['status'] == 'offline']
    if dept:
        devices = [d for d in devices if dept in d['dept']]
    return {'total': len(devices), 'dept_filter': dept, 'devices': devices}


@mcp.tool(description='按设备编号查询单台设备的当前状态、位置、固件版本、最后心跳与故障码')
async def device_detail(device_id: str) -> dict:
    for d in _load():
        if d['device_id'].lower() == device_id.lower():
            return d
    return {'error': f'未找到设备 {device_id}'}


@mcp.tool(description='修改指定设备的在线状态（写操作，仅管理端可用，用于验收时改动演示数据）')
async def update_device_status(device_id: str, status: str) -> dict:
    if not _admin_request():
        return {'error': '该工具仅对管理端开放，缺少管理端标识'}
    if status not in _ALLOWED_STATUS:
        return {'error': f'status 只能是 {"/".join(_ALLOWED_STATUS)}'}
    devices = _load()
    for d in devices:
        if d['device_id'].lower() == device_id.lower():
            before = d['status']
            d['status'] = status
            if status == 'online':
                d['fault_code'] = ''
            _save(devices)
            return {'device_id': d['device_id'], 'before': before,
                    'after': status, 'saved': True}
    return {'error': f'未找到设备 {device_id}'}


def _admin_request() -> bool:
    """本次 JSON-RPC 请求是否带上了管理端标识头

    request_context 只在 MCP 请求处理期间存在；stdio 或无 HTTP 上下文时按非管理端处理。
    """
    expected = (os.environ.get('LASSO_DEMO_ADMIN_TOKEN', '') or '').strip()
    if not expected:
        return False
    try:
        request = mcp._mcp_server.request_context.request  # noqa: SLF001 - SDK 内部句柄
    except (LookupError, AttributeError):
        return False
    if request is None:
        return False
    return (request.headers.get(ADMIN_HEADER, '') or '').strip() == expected


def _hide_write_tool():
    """覆写 FastMCP 的 tools/list 处理器：非管理端请求不披露写工具（B7 仅管理端可见）

    FastMCP 没有按请求过滤工具列表的公开入口，这里在底层 lowlevel Server 上
    重新注册同一个 handler（request_handlers 是同键覆盖），因此只影响披露，
    不影响工具本身的注册与调用逻辑。
    """
    from mcp import types as mcp_types

    async def _list_tools(_req):
        tools = await mcp.list_tools()
        if _admin_request():
            return mcp_types.ServerResult(mcp_types.ListToolsResult(tools=tools))
        return mcp_types.ServerResult(
            mcp_types.ListToolsResult(tools=[t for t in tools if t.name != _WRITE_TOOL]))

    mcp._mcp_server.request_handlers[mcp_types.ListToolsRequest] = _list_tools  # noqa: SLF001


def main() -> None:
    _hide_write_tool()
    _load()
    print(f'[demo-mcp] {SERVER_NAME} → http://{mcp.settings.host}:{mcp.settings.port}'
          f'{mcp.settings.streamable_http_path}  数据文件：{DATA_FILE}')
    mcp.run(transport='streamable-http')


if __name__ == '__main__':
    main()
