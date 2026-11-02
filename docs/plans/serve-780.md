# memsearch `serve`：官方常驻服务模式（#780）

基于 `memsearch mcp`（#834）已有代码的架构扩展。**低耦合、高内聚、多模块化**，遵循原 repo 风格（asyncio、click、dataclass、`_run`/`_safe_resolve_config` 模式）。

## 1. 目标与范围

 Issue #780 要求：

<|end|> # | 要求 | 本 PR 范围 |
|---|---|---|
| 1 | `memsearch serve --socket/--http` 常驻守护进程 | ✅ |
| 2 | 配套 thin client（`memsearch search` 等 CLI 命令自动走 daemon） | ✅ |
| 3 | 一个进程持有全部集合（解决 Milvus Lite 独占锁） | ✅ |
| 4 | watch 集成：轮询文件变更 | ✅ |
| 5 | 服务挂掉返回 exit 2，而不是空结果 | ✅ |
| 6 | 模型常驻，避免 2.2 s 重载 | ✅（天然） |
| 7 | 多 harness 访问（MCP、Hermes） | ➕ MCP 复用 daemon（可选），Hermes 通过 HTTP |

**非目标**（避免 scope 膨胀）：Hermes 专用 client、Windows 服务注册、多机部署、认证/鉴权（localhost-only）。

## 2. 设计原则

- **Transport 与业务逻辑完全分离**：socket/HTTP/stdio 三个 adapter 只负责 I/O，操作（search/index/expand/...）定义在 `ServeService` 一个类里，可复用。
- **复用现有组件**：不重写索引/搜索，`ServeService` 内部持有 `MemSearch` + `MilvusStore`，复用 `watch()`、`_cfg_to_memsearch_kwargs`。
- **单例与锁**： daemon 进程独占 Milvus Lite 文件锁；thin client 检测 daemon 存活，存活走 IPC，挂掉按 `--exit-code-on-down=2` 退出。
- **无新依赖**：stdlib `asyncio` + `socket` + `http.server`（或 `asyncio.start_server`）；HTTP 用 stdlib `http.server.ThreadingHTTPServer` 或 `aiohttp`（可选 extra `memsearch[serve]`）。
- **配置统一**：CLI 选项、`~/.memsearch/config.toml`、环境变量三者优先级与 `search` 命令一致。

## 3. 模块拆分（新增文件）

```
src/memsearch/
  serve/
    __init__.py
    service.py        # ServeService：业务操作（search/index/stats/expand/embed/rerank/watch）
    daemon.py         # Daemon：asyncio 主循环、信号、pidfile、socket/http server 启动
    socket_server.py  # Unix socket / TCP JSON-lines server（默认）
    http_server.py    # 可选 HTTP adapter（--http）
    client.py         # ThinClient：IPC 调用，供 CLI 和 MCP 复用
    protocol.py       # 请求/响应 envelope、错误码、序列化
    pidfile.py        # pidfile 管理、stale 检测、lock
    exit_codes.py     # EXIT_OK=0, EXIT_DAEMON_DOWN=2, EXIT_ERROR=1
  cli.py              # 新增 `serve` 子命令 + thin-client 自动路由
  mcp_server.py       # 可选：MCP server 走 daemon（--daemon）而非直连
```

**每个模块单一职责**，`serve/` 包可被 `cli`、`mcp_server`、外部脚本独立 import。

## 4. 协议设计

### 4.1 请求/响应（JSON lines，一行一消息）

```json
{"id": 1, "method": "search", "params": {"query": "...", "top_k": 5}}
{"id": 1, "result": {"hits": [...], "reranked": true}}
{"id": 2, "method": "index", "params": {"paths": ["/a"], "force": false}}
{"id": 2, "error": {"code": "INDEX_FAILED", "message": "..."}}
```

- `id`：客户端生成的整数，用于匹配响应。
- `method`：`search` / `index` / `index_with_report` / `stats` / `expand` / `embed` / `rerank` / `watch` / `shutdown` / `ping` / `status`。
- `params`：方法参数，JSON 对象。
- 响应：`result` 或 `error` 二选一。
- 流式：`watch` 事件通过 `{"id": N, "event": "file_changed", "data": {...}}` 推送（长连接）。

### 4.2 错误码

| code | 含义 |
|---|---|
| `DAEMON_DOWN` | 客户端检测到 daemon 未运行（本地错误，不发请求） |
| `INVALID_PARAMS` | 参数类型/范围错误 |
| `NOT_FOUND` | 集合/文件不存在 |
| `INDEX_FAILED` | 索引过程异常 |
| `SHUTTING_DOWN` | daemon 正在退出，拒绝新请求 |
| `INTERNAL` | 未分类错误 |

### 4.3 传输

- **Unix socket**（Linux/macOS）：`~/.memsearch/serve.sock`，文件权限 0600。
- **TCP**（Windows / `--tcp`）：`127.0.0.1:19834`（可配）。
- **HTTP**（`--http`）：`POST /rpc` 接收 JSON body，返回 JSON；`GET /health` 健康检查。便于 Hermes 等非 Python 客户端。
- 默认：Unix socket（Linux/macOS）/ TCP（Windows）。

## 5. 核心组件设计

### 5.1 `ServeService`（`serve/service.py`）

```python
class ServeService:
    def __init__(self, cfg: MemSearchConfig, paths: list[str]) -> None:
        self._cfg = cfg
        self._paths = paths
        self._ms: MemSearch | None = None
        self._watcher: FileWatcher | None = None
        self._lock = asyncio.Lock()  # 串行化 index 操作

    async def start(self) -> None:
        self._ms = MemSearch(self._paths, **_cfg_to_memsearch_kwargs(self._cfg))
        self._watcher = self._ms.watch(on_event=self._on_event, on_error=self._on_error)
        # initial index
        await self._ms.index_with_report()

    async def search(self, query, top_k=5, reranker_model=None, ...) -> dict: ...
    async def index(self, paths, force=False) -> dict: ...
    async def stats(self) -> dict: ...
    async def expand(self, chunk_hash, ...) -> dict: ...
    async def embed(self, texts) -> list[list[float]]: ...
    async def rerank(self, query, documents) -> list[dict]: ...
    async def shutdown(self) -> None: ...

    # 状态
    @property
    def is_ready(self) -> bool: ...
    @property
    def collection(self) -> str: ...
```

- `MemSearch` 的异步方法直接 `await`；同步方法（如 `store()`）用 `asyncio.to_thread`。
- `search`/`index` 持有 `asyncio.Lock`，避免 Milvus 并发写入冲突。

### 5.2 `Daemon`（`serve/daemon.py`）

```python
async def run_daemon(cfg, paths, socket_path, http_port, pidfile):
    service = ServeService(cfg, paths)
    await service.start()
    server = await start_server(...)  # socket 或 http
    # 写 pidfile
    await server.serve_forever()
    # SIGTERM/SIGINT -> graceful shutdown
```

- 信号处理：`loop.add_signal_handler(SIGTERM, ...)` / `SIGINT`。
- Graceful shutdown：停 watcher → close store → 删除 pidfile → 退出。
- 日志：用 `logging`，输出到 stderr（daemon 模式）或文件（`--log-file`）。

### 5.3 `ThinClient`（`serve/client.py`）

```python
class ThinClient:
    def __init__(self, socket_path=None, tcp_host=None, tcp_port=None): ...

    async def call(self, method, params, timeout=30) -> dict:
        # 连接 -> 发请求 -> 读响应 -> 关闭
        # 连接失败 -> raise DaemonDownError

    def is_alive(self) -> bool:
        # 快速 ping，用于 CLI 自动路由
```

- 自动路由逻辑：`_safe_resolve_config` 之后，CLI 检查 `ThinClient.is_alive()`；若存活且 `--no-daemon` 未指定，则 `await client.call(...)`；否则本地执行（当前行为）。
- 环境变量 `MEMSEARCH_NO_DAEMON=1` 强制本地执行（调试用）。

### 5.4 CLI 集成（`cli.py`）

新增命令：

```bash
memsearch serve [PATHS...]            # 启动 daemon
  --socket PATH                      # Unix socket / TCP 端口
  --http PORT                        # 启用 HTTP adapter
  --pidfile PATH                     # pidfile 路径
  --foreground                       # 前台运行（默认 daemonize）
  --exit-code-on-down INT             # daemon 挂掉时的退出码（默认 2）

memsearch serve status                 # 检查 daemon 状态
memsearch serve stop                   # 停止 daemon
```

已有命令（`search`、`index`、`stats`、`expand`）在 daemon 存活时自动走 ThinClient，加 `--no-daemon` 强制本地。

### 5.5 MCP server 集成（`mcp_server.py`）

新增 `--daemon` 选项：若 daemon 存活，MCP server 走 ThinClient 而非直连 `MemSearch`。这样 MCP 客户端也能复用常驻模型。

## 6. 文件锁与冲突处理

- daemon 启动时尝试创建 pidfile（`O_CREAT | O_EXCL`），失败则报错退出（已有实例）。
- Milvus Lite 文件锁：daemon 独占；CLI thin client 不直接打开 Milvus，避免冲突。
- `watch` 由 daemon 内部启动，文件变更自动索引，客户端无需 watch。

## 7. 测试策略

- `tests/test_serve_protocol.py`：请求/响应序列化、错误码。
- `tests/test_serve_client.py`：ThinClient 连接、超时、daemon down 检测（mock socket）。
- `tests/test_serve_service.py`：ServeService 操作（mock MilvusStore / MemSearch）。
- `tests/test_serve_daemon.py`：daemon 启动/停止、pidfile、信号处理（集成测试，可选）。
- `tests/test_cli_serve.py`：CLI 命令解析、`--no-daemon` 路由。

所有测试 hermetic，不依赖真实 Milvus/网络。

## 8. 实施顺序（可独立 commit）

1. `serve/protocol.py` + `serve/pidfile.py` + `serve/exit_codes.py`（纯数据/工具，无依赖）
2. `serve/service.py`（ServeService，复用 MemSearch/MilvusStore）
3. `serve/socket_server.py` + `serve/http_server.py`（I/O adapter）
4. `serve/daemon.py`（主循环 + 信号）
5. `serve/client.py`（ThinClient）
6. `cli.py`：新增 `serve` 子命令 + thin-client 自动路由
7. `mcp_server.py`：`--daemon` 选项
8. 测试 + 文档

每步独立可测试，低风险。

## 9. 与现有代码的耦合点（最小改动）

- `MemSearch`：无需修改，ServeService 直接使用其 public API。
- `MilvusStore`：无需修改，通过 `MemSearch.store()` 获取。
- `watch()`：无需修改，daemon 内部调用。
- `_safe_resolve_config` / `_cfg_to_memsearch_kwargs`：复用，不重复实现。
- `mcp_server.py`：只加 `--daemon` 选项，原有直连路径保留。

## 10. 风险与缓解

| 风险 | 缓解 |
|---|---|
| Milvus Lite 文件锁冲突 | daemon 独占；thin client 不打开 Milvus |
| daemon 崩溃导致客户端挂起 | ThinClient 设置超时；失败返回 exit 2 |
| Windows 无 Unix socket | 默认 TCP on Windows |
| 多 daemon 实例 | pidfile 原子创建 + 检测 |
| watch 事件风暴 | 复用现有 debounce 机制 |

## 11. 交付标准

- `memsearch serve` 启动后，`memsearch search` 自动走 daemon（无额外配置）。
- `memsearch serve stop` 干净退出，pidfile 删除，Milvus 锁释放。
- daemon 崩溃时 `memsearch search` 返回 exit 2，不返回空结果。
- 85+ 测试通过，ruff clean。
- 文档：`docs/serve.md` + CLI reference 更新。
