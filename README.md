# 采购公告采集 API

传入**采购公告列表页 URL** 与**项目编号**，自动抓取该页指向的同站点原公告与更正公告，
从详情页提取发布时间、投标截止时间与取消采购信息，按发布时间合并后返回：
当前是否可投标、有效截止时间、项目状态以及全部来源链接。

## 业务规则

| 规则 | 实现位置 |
| --- | --- |
| 只采集列表页指向的**同站点**详情链接，外站链接、登录/导航链接自动忽略 | `app/parser.py::extract_detail_links` |
| 按**发布时间**从早到晚合并原公告与更正公告 | `app/merger.py::merge_notices` |
| 发布时间**带时区偏移**（如 `2026-11-04T21:00:00-05:00`）时按**实际时刻**排序，跨时区发布的更正不会因字面时间先后被排错；偏移量在结果中原样保留 | `app/parser.py::parse_datetime` + `app/merger.py` |
| 同一项目**混有带/不带时区**的发布时间时：请求提供 `site_timezone`（IANA 时区名，如 `Asia/Shanghai`）→ 无时区时间按站点时区解释后参与排序；未提供 → 状态 `pending` 待核实、`effective_deadline=null` | `app/merger.py` |
| 全部公告均**不带时区**时，沿用原有本地时间口径直接比较，不附加时区 | `app/merger.py` |
| 更正公告**未提及截止时间**时，沿用此前已生效的截止时间 | `app/merger.py` |
| 更正公告给出新截止时间（如延期）时，以新时间覆盖 | `app/merger.py` |
| 公告明确**取消采购** → 状态 `closed`、不可投标，不再返回有效截止时间（旧时间仅在公告明细中保留） | `app/merger.py` |
| **任一关联详情页**抓取/解析失败（404、超时、结构异常等）→ 状态 `pending` 待核实，`effective_deadline` 强制为 `null`，**绝不把旧时间当作最终期限** | `app/merger.py` + `app/collector.py` |
| 发布时间缺失、缺少原公告、全部公告均无截止时间等信息链不完整情况，同样标记 `pending` | `app/merger.py` |

### 项目状态（`status`）

- `open`：可投标，`effective_deadline` 为合并后的有效截止时间；
- `closed`：采购已取消，`can_bid=false`，无有效截止时间；
- `pending`：待核实，存在关联页面失败或信息不完整，`effective_deadline=null`；
- `not_found`：列表页未发现与该项目编号相关的公告。

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 入口、路由、浏览器演示页
│   ├── collector.py   # 采集编排：列表发现 → 并发抓取 → 归属判定 → 合并
│   ├── parser.py      # HTML 解析：同站链接发现、三层字段提取、取消识别
│   ├── merger.py      # 业务合并规则（时间继承、延期覆盖、取消、待核实）
│   └── models.py      # 请求/响应 Pydantic 模型
├── sample-site/       # 本地示例公告站点（nginx 静态托管）
│   ├── list.html      # 公告列表页（含外站/登录干扰链接）
│   └── detail/        # 3 个项目的原公告/更正/取消详情页
├── nginx/default.conf # 示例站点 nginx 配置（缺失页面返回真实 404）
├── tests/             # 解析/合并单测 + 本地静态服务器端到端测试（48 个用例）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动后：

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| 公告采集 API | http://localhost:8000 | Swagger 文档：http://localhost:8000/docs |
| 浏览器演示页 | http://localhost:8000/ | 填表即可发起采集 |
| 示例公告站点 | http://localhost:8080/list.html | nginx 托管的静态采购公告 |

停止：`docker compose down`。

## API 用法

### POST（推荐）

```bash
curl -X POST http://localhost:8000/api/v1/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url":"http://localhost:8080/list.html","project_code":"GSB-2026-001"}'
```

跨时区/混合时区公告可携带 `site_timezone`（IANA 时区名），用于解释不带偏移量的发布时间：

```bash
curl -X POST http://localhost:8000/api/v1/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url":"http://localhost:8080/list.html","project_code":"GSB-2026-005","site_timezone":"Asia/Shanghai"}'
```

### GET（便于浏览器快速验证）

```bash
curl "http://localhost:8000/api/v1/collect?list_url=http://localhost:8080/list.html&project_code=GSB-2026-001"
# 需要站点时区时：
curl "http://localhost:8000/api/v1/collect?list_url=http://localhost:8080/list.html&project_code=GSB-2026-005&site_timezone=Asia/Shanghai"
```

响应关键字段：

```json
{
  "project_code": "GSB-2026-001",
  "status": "open",
  "can_bid": true,
  "effective_deadline": "2026-11-15T14:00:00",
  "cancelled": false,
  "notices": [ { "kind": "original", "published_at": "...", "deadline": "..." } ],
  "sources": [ { "role": "list", "url": "..." }, { "role": "correction", "url": "..." } ],
  "warnings": []
}
```

公告带时区偏移时，`published_at` / `deadline` / `effective_deadline` 会保留偏移量，例如
`"effective_deadline": "2026-11-25T10:00:00-05:00"`。

## 示例站点内置的五个演示项目

| 项目编号 | 场景 | 预期结果 |
| --- | --- | --- |
| `GSB-2026-001` | 原公告 → 澄清更正（**未提截止时间，沿用原时间**）→ 延期更正（**新时间覆盖**） | `open`，有效截止时间 `2026-11-15 14:00` |
| `GSB-2026-002` | 原公告 → 取消公告（预算收回） | `closed`，不可投标，无有效截止时间 |
| `GSB-2026-003` | 原公告 → 更正页**已下线（404）** | `pending` 待核实，`effective_deadline=null`，来源中列出失败链接 |
| `GSB-2026-004` | **跨时区发布**：原公告（UTC+8）→ 上海更正（UTC+8，延期至 11-20）→ 纽约更正（UTC-5，字面日期更早但**实际时刻更晚**，延期至 11-25） | `open`，按实际时刻排序，有效截止时间 `2026-11-25T10:00:00-05:00`（保留偏移量） |
| `GSB-2026-005` | **混合时区口径**：原公告无时区（老系统）→ 更正带 UTC+8 偏移（新平台） | 提供 `site_timezone=Asia/Shanghai` → `open`，截止 `2026-11-24T15:00:00+08:00`；不提供 → `pending`，`effective_deadline=null` |

另外列表页还包含：外站链接、登录导航链接、其他项目公告（`GSB-2026-099`）、
无关项目的坏链接（`GSB-2025-888`），用于验证同站过滤、项目归属判定与失败隔离。

## 详情页解析策略（三层回退）

1. **结构化标记**：`data-field="published-at|deadline|cancelled|..."` 元素或
   `<meta property="article:published_time">`；
2. **标签匹配**：`发布时间 / 投标截止时间 / 响应文件递交截止时间 / 取消原因` 等
   中文标签，兼容 `th/td`、`dt/dd`、`li/p` 等布局，支持
   `2026-11-10 09:30`、`2026年11月15日 14时00分` 等常见写法，
   以及 `2026-11-04T21:00:00-05:00`、`2026-11-05T09:00:00+08:00`（含 `Z`/`+0800`）
   等带 ISO 8601 时区偏移的写法——偏移量会被保留，用于跨时区的实际时刻排序；
3. **正文正则兜底**：在含“截止/递交/提交/受理”等时限语义的块级元素中识别日期，
   并排除“发布时间”句、资质业绩说明中的历史日期（如“近三年（2023年1月1日起）”）。

取消采购识别支持 `data-field="cancelled"` 标记、取消原因标签以及
“本项目取消采购 / 终止本次招标”等显式句式。

> 接入真实采购平台时，若页面结构不同，优先在详情页模板中补 `data-field` 标记，
> 或在 `app/parser.py` 顶部的标签/关键词元组中登记该平台的字段叫法。

## 本地开发与测试（不使用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 终端 A：托管示例公告页
cd sample-site && python -m http.server 8080
# 终端 B：启动 API
uvicorn app.main:app --reload --port 8000

# 测试（自带静态服务器，无需外部依赖）
pytest -q
```

## 设计说明

- 列表页抓取失败直接返回 `502`；详情页失败不影响整体响应，而是按业务规则沉淀为
  `pending` + 失败来源链接，便于投标专员人工核实。
- 时区处理分三档：全部公告带偏移 → 按实际时刻（换算同一时间基准）排序；
  混有带/不带偏移 → 用请求提供的 `site_timezone`（IANA 名，经 `zoneinfo` 解析，
  镜像内通过 `tzdata` 包保证时区数据可用）解释无时区时间，未提供则 `pending` 且期限置空；
  全部不带偏移 → 保持原有本地时间口径，不附加时区。待核实路径的排序键对混合口径做了
  归一，任何情况下都不会因“naive/aware 不可比较”而抛错。
- 关联归属判定结合详情页项目编号与列表行上下文（含编号列），
  即使详情页下线（只能看到列表行）也能正确判定失败是否与目标项目相关；
  无关项目的坏链接不会污染查询结果。
- 详情页并发抓取（上限 5），统一超时、自定义 UA、跟随重定向。
