# resumable-page-fetcher

不直接访问网络的 Python 游标分页采集库。HTTP 请求由调用方通过 `Transport`
协议注入，库本身只负责重试、去重、游标推进和断点续跑；所有时间也通过
`Clock` 注入，测试可以用 `FakeClock` 完全脱离真实时间。

## 核心抽象

- `Transport.fetch(PageRequest) -> PageResponse`：调用方实现，把真实 HTTP
  响应翻译成 `PageResponse`，失败时抛 `RateLimitError`(429)、
  `ServerError`(5xx) 或 `StaleResponseError`(已取消/已过期)。
- `CheckpointStore`：持久化接口，内置 `JsonlCheckpointStore`
  （`records.jsonl` + 原子替换的 `checkpoint.json`）。
- `Paginator`：采集器，`iter_pages()` 逐页产出新记录（每页先落库再产出，
  可随时停止），`iter_records()` 无网络重放全部已提交记录，`run()`
  跑到最后一页并返回全部记录。

## 快速示例

```python
from resumable_page_fetcher import (
    JsonlCheckpointStore, Paginator, PageRequest, PageResponse,
    RateLimitError, ServerError, parse_retry_after, RetryPolicy, SystemClock,
)


class HttpTransport:
    def __init__(self, client):
        self.client = client

    def fetch(self, request: PageRequest) -> PageResponse:
        response = self.client.get(
            "/items",
            params={"cursor": request.cursor, "page_size": request.page_size},
            timeout=10,
        )
        if response.status_code == 429:
            raise RateLimitError(
                retry_after=parse_retry_after(response.headers.get("Retry-After"))
            )
        if 500 <= response.status_code < 600:
            raise ServerError(status_code=response.status_code)
        payload = response.json()
        return PageResponse(
            items=tuple(payload["items"]),
            next_cursor=payload.get("next_cursor"),
            expires_at=payload.get("expires_at_monotonic"),
        )


paginator = Paginator(
    HttpTransport(client),
    JsonlCheckpointStore("./data/run-1"),
    page_size=100,
    item_key=lambda record: record["id"],
    retry_policy=RetryPolicy(
        max_attempts=5,
        base_delay=1.0,
        factor=2.0,
        max_delay=30.0,
        jitter=0.25,
        jitter_seed=42,
    ),
    clock=SystemClock(),
)

for new_records in paginator.iter_pages():
    process(new_records)

# 进程重启后用同一目录重新构造 Paginator 即可继续；已完成的 run 不会再发请求
all_records = list(Paginator(HttpTransport(client), store).iter_records())
```

## 重试策略

- **429 限流**：若 `RateLimitError` 带 `retry_after`（可用
  `parse_retry_after` 解析秒数或 HTTP-date 头，非法值返回 `None`），
  严格等待该秒数；缺省或无法解析时回退到指数退避。
- **5xx 服务端错误**：指数退避 `base_delay * factor ** attempt`，
  并用 `max_delay` 封顶。
- **抖动**：每次等待附加 `random.Random(jitter_seed).random() * jitter`，
  种子由调用方显式传入，相同种子产生完全可复现的等待序列，测试不依赖
  真实时间（`FakeClock.sleep` 只推进虚拟时钟并记录等待值）。
- **重试上限**：同一页最多尝试 `max_attempts` 次，耗尽后抛
  `RetryExhaustedError`（保留最后一次错误），此时不写任何新状态，
  checkpoint 仍指向上一个已提交页，下次运行从该页重试。
- **已取消/已过期响应**：`StaleResponseError` 以及 `PageResponse` 上的
  `cancelled=True` / `expires_at <= clock.now()` 不会被重试，也绝不推进
  游标或提交 checkpoint；调用方可稍后用同一 checkpoint 重新开跑。

## 恢复与一致性策略

- **提交粒度**：只有一整页抓取成功并完成去重后才提交。提交顺序为
  先追加 `records.jsonl` 并 `fsync`，再把新 checkpoint 写入临时文件、
  `fsync` 后 `os.replace` 原子替换，最后 `fsync` 目录。
- **写库前崩溃**：若进程在 checkpoint 落盘前死亡，重启时 `load()` 会按
  checkpoint 中记录的 `record_count` 截断数据文件的未提交尾部（孤儿
  记录），保证“可见记录必有对应 checkpoint”，不会重复入库。
- **断点续跑**：checkpoint 保存 `next_cursor`、已见游标 token、去重键
  集合、连续空页计数和 `done` 标志；重启后从 `next_cursor` 继续，
  而不是第一页。
- **游标处理**：游标对库不透明，只做原样存储与回传，不做大小比较；
  循环检测使用规范化 JSON token 的相等判断，因此字典、列表等不可
  排序游标也可用（要求可 JSON 序列化）。
- **去重**：默认对整条记录做规范化 JSON 取键，也可注入 `item_key`
  （如业务主键）；页内与跨页重复都会被过滤，去重状态随 checkpoint
  持久化，重启后仍然生效。
- **重复页 / 游标循环**：接口返回已抓取过的 `next_cursor`（包括重复
  页）立即抛 `CursorLoopError`，避免死循环。
- **空页与最后一页**：空页照常提交并累计 `empty_streak`，连续空页
  超过 `max_empty_pages` 抛 `EmptyPageLimitError`，防止接口永不返回
  终止游标；`next_cursor is None` 视为最后一页，提交 `done=True`，
  后续运行直接重放数据、不再请求接口。
- **可重复迭代**：`iter_pages()` 是生成器，支持消费几页后关闭，稍后
  用新实例继续；`iter_records()` 任何时候都能离线重放已提交数据。

## 测试

测试全部基于假接口与虚拟时钟，不访问外部服务、不消耗真实时间：

```bash
python3 -m unittest
```

覆盖：正常多页采集、429 带/不带 `Retry-After`、5xx 退避与封顶、
种子抖动可复现、重试耗尽、取消/过期响应不推进游标、跨页与页内去重、
重复页与游标循环、空页与无限空页、最后一页、写库前崩溃截断与续跑、
中途停止后续跑、不透明字典游标、损坏 checkpoint 报错。
