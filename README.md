# 振动突增告警 API（Vibration Spike Alert API）

Python 3.11 + FastAPI + PostgreSQL。按设备接收振动采样读数，用每条读数**此前 60 分钟**
有效幅值的**精确中位数**作为基线，幅值**严格超过基线 3 倍**时判定为突增（hit），
并把时间相邻的 hit 合并为告警。读数、告警、变更日志在**同一事务**中提交。

## 一、启动（Docker Compose 一键启动）

```bash
docker compose up --build
```

- API：<http://localhost:8000>
- 交互文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/healthz>
- PostgreSQL：`localhost:55432`（库 / 用户 / 密码均为 `vibration`）

停止并清空数据：

```bash
docker compose down -v
```

不用 Docker 时也可本地运行：

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://vibration:vibration@localhost:55432/vibration
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 二、如何访问告警结果

| 接口 | 用途 |
| --- | --- |
| `POST /readings` | 按设备批量上报读数；响应逐条返回基线与判定 |
| `GET /alerts?device_id=...` | 查询设备的**当前**告警（支持 `limit`/`offset`） |
| `GET /changes?device_id=...&cursor=...` | 凭游标续读告警的**新增 / 更新 / 撤销** |
| `GET /healthz` | 健康检查 |

游标用法：首次 `cursor=0`，之后把上一次响应的 `next_cursor` 原样带回；
`has_more=true` 表示后面还有数据。告警按事件时间排序；读数按
**（事件时间，同秒则采样 ID）**排定先后，仅当**相邻两条读数均命中、间隔不超过
5 分钟（含 300 秒）且中间没有非命中读数（normal/unknown）**时才合并，告警 ID
取该段首个命中读数的采样 ID。

## 三、实际请求 / 响应示例（均可直接复现）

下面主要用设备 `pump-7` 演示完整流程（拆分 / 重新合并示例用 `pump-8`）。
`event_time` 为 UTC 整秒（RFC 3339）。

### 1) 前 20 条平静数据：历史不足 20 条 → 基线未知

```bash
curl -s -X POST http://localhost:8000/readings \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"pump-7","readings":[
    {"sample_id":"s00","rev":0,"event_time":"2026-10-01T00:00:00Z","amplitude":1.0},
    {"sample_id":"s01","rev":0,"event_time":"2026-10-01T00:00:01Z","amplitude":1.0}
  ]}'
```

```json
{
  "device_id": "pump-7",
  "results": [
    {"sample_id":"s00","rev":0,"event_time":"2026-10-01T00:00:00Z","amplitude":1.0,
     "status":"accepted","baseline":null,"verdict":"unknown"},
    {"sample_id":"s01","rev":0,"event_time":"2026-10-01T00:00:01Z","amplitude":1.0,
     "status":"accepted","baseline":null,"verdict":"unknown"}
  ]
}
```

`verdict` 取值：`hit`（严格超过 3 倍基线）、`normal`（未超过）、`unknown`（此前
60 分钟有效幅值不足 20 条）。基线窗口含左端、不含同秒：即取时间落在
`[event_time-60min, event_time)` 的读数，同一秒的其他读数不参与基线。

### 2) 第 21 条：已有 20 条历史，中位数 1.0

```bash
curl -s -X POST http://localhost:8000/readings \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"pump-7","readings":[
    {"sample_id":"s20","rev":0,"event_time":"2026-10-01T00:00:20Z","amplitude":1.0}
  ]}'
```

```json
{"device_id":"pump-7","results":[
  {"sample_id":"s20","rev":0,"event_time":"2026-10-01T00:00:20Z","amplitude":1.0,
   "status":"accepted","baseline":1.0,"verdict":"normal"}]}
```

（把同样的 20 条 `s00`–`s19` 补全后再继续下面的步骤；示例脚本见
`tests/test_api.py`。）

### 3) 恰好 3 倍不命中，严格超过才命中

```bash
# 幅值恰好 = 3 × 1.0 → normal
curl -s -X POST http://localhost:8000/readings -H 'Content-Type: application/json' \
  -d '{"device_id":"pump-7","readings":[
    {"sample_id":"s21","rev":0,"event_time":"2026-10-01T00:00:21Z","amplitude":3.0}]}'
# 幅值 3.01 > 3.0 → hit
curl -s -X POST http://localhost:8000/readings -H 'Content-Type: application/json' \
  -d '{"device_id":"pump-7","readings":[
    {"sample_id":"s22","rev":0,"event_time":"2026-10-01T00:00:22Z","amplitude":3.01}]}'
```

```json
{"device_id":"pump-7","results":[
  {"sample_id":"s22","rev":0,"event_time":"2026-10-01T00:00:22Z","amplitude":3.01,
   "status":"accepted","baseline":1.0,"verdict":"hit"}]}
```

### 4) 60 秒后再次命中 → 合并为同一条告警（间隔 ≤ 5 分钟）

```bash
curl -s -X POST http://localhost:8000/readings -H 'Content-Type: application/json' \
  -d '{"device_id":"pump-7","readings":[
    {"sample_id":"s23","rev":0,"event_time":"2026-10-01T00:01:22Z","amplitude":3.01}]}'

curl -s 'http://localhost:8000/alerts?device_id=pump-7'
```

```json
{
  "alerts": [
    {
      "alert_id": "s22",
      "device_id": "pump-7",
      "first_sample_id": "s22",
      "last_sample_id": "s23",
      "starts_at": "2026-10-01T00:00:22Z",
      "ends_at": "2026-10-01T00:01:22Z",
      "hit_count": 2,
      "max_amplitude": 3.01,
      "updated_at": "2026-10-06T03:11:53.464647Z"
    }
  ]
}
```

若两次命中相隔超过 5 分钟（>300 秒）或中间夹了非命中读数，则分成两条告警，
各自的 `alert_id` 为该段首个命中读数的采样 ID。

### 5) 查看变更（游标续读）

```bash
curl -s 'http://localhost:8000/changes?device_id=pump-7&cursor=0'
```

```json
{
  "changes": [
    {"id":20,"device_id":"pump-7","alert_id":"s22","op":"created",
     "changed_at":"2026-10-06T03:11:53.464647Z","data":{"...":"首段快照"}},
    {"id":21,"device_id":"pump-7","alert_id":"s22","op":"updated",
     "changed_at":"2026-10-06T03:11:53.464647Z","data":{"...":"合并后的快照"}}
  ],
  "next_cursor": 21,
  "has_more": false
}
```

`op`：`created`（新告警）、`updated`（首末读数 / 计数 / 峰值变化，ID 保持不变）、
`revoked`（该段不再存在）。翻下一页：`?cursor=21`。

### 6) 夹入非命中读数：告警拆分；改回命中：重新合并（设备 pump-8）

两条命中相距不足 5 分钟、但中间夹了 normal/unknown 读数时**不会**合并。用
`pump-8` 演示（先灌 20 条平静数据 `k00`–`k19`：`2026-10-01T00:00:00Z` 起逐秒
一条、幅值 1.0，同第 1 步 `s00`–`s19` 的格式）：

```bash
# 两条命中相隔 60 秒 -> 合并为一条告警 x1
curl -s -X POST http://localhost:8000/readings -H 'Content-Type: application/json' \
  -d '{"device_id":"pump-8","readings":[
    {"sample_id":"x1","rev":0,"event_time":"2026-10-01T00:01:00Z","amplitude":3.01},
    {"sample_id":"x2","rev":0,"event_time":"2026-10-01T00:02:00Z","amplitude":3.01}]}'
```

```json
{"device_id":"pump-8","results":[
  {"sample_id":"x1","rev":0,"event_time":"2026-10-01T00:01:00Z","amplitude":3.01,
   "status":"accepted","baseline":1.0,"verdict":"hit"},
  {"sample_id":"x2","rev":0,"event_time":"2026-10-01T00:02:00Z","amplitude":3.01,
   "status":"accepted","baseline":1.0,"verdict":"hit"}]}
```

在两条命中**之间**补一条普通读数：

```bash
curl -s -X POST http://localhost:8000/readings -H 'Content-Type: application/json' \
  -d '{"device_id":"pump-8","readings":[
    {"sample_id":"mid","rev":0,"event_time":"2026-10-01T00:01:30Z","amplitude":1.0}]}'

curl -s 'http://localhost:8000/alerts?device_id=pump-8'
```

`mid` 判定为 `normal`，原告警拆成两条（各自的 `alert_id` 为该段首个命中的采样 ID）：

```json
{
  "alerts": [
    {"alert_id":"x1","device_id":"pump-8","first_sample_id":"x1","last_sample_id":"x1",
     "starts_at":"2026-10-01T00:01:00Z","ends_at":"2026-10-01T00:01:00Z",
     "hit_count":1,"max_amplitude":3.01,"updated_at":"2026-10-06T17:58:31.732473Z"},
    {"alert_id":"x2","device_id":"pump-8","first_sample_id":"x2","last_sample_id":"x2",
     "starts_at":"2026-10-01T00:02:00Z","ends_at":"2026-10-01T00:02:00Z",
     "hit_count":1,"max_amplitude":3.01,"updated_at":"2026-10-06T17:58:31.732473Z"}
  ]
}
```

变更流按顺序记录这次拆分：`created(x1)`（最初合并段）→ `updated(x1)`（首段收缩）
→ `created(x2)`（拆出的新段）。

再用**更高修订**把分隔读数改成命中，两段重新合并：

```bash
curl -s -X POST http://localhost:8000/readings -H 'Content-Type: application/json' \
  -d '{"device_id":"pump-8","readings":[
    {"sample_id":"mid","rev":1,"event_time":"2026-10-01T00:01:30Z","amplitude":3.01}]}'

curl -s 'http://localhost:8000/alerts?device_id=pump-8'
```

```json
{
  "alerts": [
    {"alert_id":"x1","device_id":"pump-8","first_sample_id":"x1","last_sample_id":"x2",
     "starts_at":"2026-10-01T00:01:00Z","ends_at":"2026-10-01T00:02:00Z",
     "hit_count":3,"max_amplitude":3.01,"updated_at":"2026-10-06T17:58:31.818418Z"}
  ]
}
```

此时 `GET /changes?device_id=pump-8` 的完整序列为：

```json
{
  "changes": [
    {"id":42,"device_id":"pump-8","alert_id":"x1","op":"created",
     "changed_at":"2026-10-06T17:58:31.679396Z","data":{"...":"合并段 {x1,x2}"}},
    {"id":43,"device_id":"pump-8","alert_id":"x1","op":"updated",
     "changed_at":"2026-10-06T17:58:31.732473Z","data":{"...":"首段收缩为 {x1}"}},
    {"id":44,"device_id":"pump-8","alert_id":"x2","op":"created",
     "changed_at":"2026-10-06T17:58:31.732473Z","data":{"...":"拆出的 {x2}"}},
    {"id":45,"device_id":"pump-8","alert_id":"x2","op":"revoked",
     "changed_at":"2026-10-06T17:58:31.818418Z","data":{"...":"重新合并,{x2} 段撤销"}},
    {"id":46,"device_id":"pump-8","alert_id":"x1","op":"updated",
     "changed_at":"2026-10-06T17:58:31.818418Z","data":{"...":"合并段 {x1,mid,x2}"}}
  ],
  "next_cursor": 46,
  "has_more": false
}
```

### 7) 更正：高修订改幅值，受影响窗口重算，旧告警撤销

```bash
curl -s -X POST http://localhost:8000/readings -H 'Content-Type: application/json' \
  -d '{"device_id":"pump-7","readings":[
    {"sample_id":"s22","rev":1,"event_time":"2026-10-01T00:00:22Z","amplitude":1.0}]}'
```

```json
{"device_id":"pump-7","results":[
  {"sample_id":"s22","rev":1,"event_time":"2026-10-01T00:00:22Z","amplitude":1.0,
   "status":"accepted","baseline":1.0,"verdict":"normal"}]}
```

此后 `s22` 不再命中，以 `s22` 为首的告警被撤销（`s23` 仍命中时会成为一条单独
告警）。`GET /changes` 按顺序可见：`created → updated → revoked(s22) →
created(s23)`。

修订规则：

- **更高修订**可改时间或幅值；系统对受影响窗口
  （`最早改动时间` 到 `最晚改动时间 + 60 分钟`内的当前读数）精确重算基线、判定与
  全部告警。
- **旧修订忽略**：`rev` 小于已存值时返回 `"status":"stale"`，并回传当前生效内容。
- **等修订异内容冲突**：整批回滚、一条数据都不写，返回 `409` 及冲突明细：

```json
{
  "device_id": "pump-7",
  "error": "revision conflict",
  "results": [
    {"sample_id":"s20","rev":0,"event_time":"2026-10-01T00:00:20Z","amplitude":9.0,
     "status":"conflict","stored_rev":0,
     "stored_event_time":"2026-10-01T00:00:20Z","stored_amplitude":1.0,
     "baseline":null,"verdict":null}
  ]
}
```

- **同修订同内容 = 重试**：返回 `"status":"duplicate"`，**不会重复产生变更**。
  并发相同请求也由设备级事务咨询锁串行化，实测 8 路并发同请求只有 1 次 `accepted`、
  7 次 `duplicate`，变更表无重复行。

### 8) 输入约束（违例返回 422）

- `readings` 非空，单批最多 1000 条；同一批次内 `sample_id` 不可重复；
- `rev` 为非负整数；
- `event_time` 必须是带时区的 UTC **整秒**；
- `amplitude` 为有限非负数（拒绝负数、`NaN`、`Infinity`）。

## 四、性能

单设备累计 10 万条读数后，100 条纯追加批次实测（PostgreSQL 16，fsync=off）
延迟：最小 30 ms、中位 32 ms、**P95 49 ms**（要求 P95 ≤ 1 s）。10 万条初始灌入
约 18 秒（每批 1000）。

实现要点：

- 基线重算用**滑动窗口双堆 + 惰性删除**，对时间轴做一次 O(n log n) 扫描，避免对
  每条读数单独做窗口聚合；
- 告警重建**不扫全序列**：只取该设备的命中读数，再对"间隔 ≤ 5 分钟、可能需要
  合并"的相邻命中对所覆盖的时间区间做一次非命中读数范围查询（区间外的非命中
  不可能影响合并结果——间隔超过 5 分钟的相邻命中必然拆分）。纯追加、无命中的
  负载下这次额外查询完全不发生；相比全序列扫描，追加路径的开销与命中数量而非
  设备总读数数挂钩；
- 每设备在事务内 `pg_advisory_xact_lock(hashtextextended(device_id,0))` 串行化，
  并发批次不丢数据，重试不重复发变更；
- 批量写入走 psycopg3 pipeline；关键索引：
  `readings(device_id,event_time,sample_id) INCLUDE (amplitude)`、
  `readings(device_id,event_time,sample_id) INCLUDE (amplitude) WHERE verdict='hit'`
  （部分索引，无命中时为空、零维护代价）、
  `alerts(device_id,starts_at,alert_id)`、`changes(id)`。

## 五、测试

需要一个运行中的 API 与 PostgreSQL：

```bash
BASE_URL=http://localhost:8000 pytest tests/ -s
```

- `tests/test_sliding_median.py`：滑动窗口中位数与 `statistics.median` 的随机对照
  （含重复值、窗口左右边界）；
- `tests/test_api.py`：基线未知 / 命中、严格 3 倍、5 分钟合并与分裂、**夹入
  normal/unknown 拆段、同秒按采样 ID 判定相邻、高修订改回命中后重新合并**、
  旧修订忽略、等修订冲突整批回滚、重试幂等、更正重算与撤销、游标续读、输入校验，
  以及 10 万条 + 追加批次 P95 性能基准。

## 六、数据模型

| 表 | 说明 |
| --- | --- |
| `readings` | 主键 `(device_id, sample_id)`；保存当前最高修订的时间 / 幅值及其基线、判定 |
| `alerts` | 当前告警；主键 `(device_id, alert_id)`，每次提交按最新判定重建 |
| `changes` | 仅追加的告警变更日志，`id` 为全局单调游标 |
