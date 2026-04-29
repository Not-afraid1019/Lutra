# Lutra

本地 AI 编码助手 —— 通过飞书接收指令，使用 Claude 读写代码、执行命令、分析和修复 JIRA issue、自动处理 GitLab MR Review。

## 架构

```
飞书用户 ──WebSocket──▷ feishu.py ──▷ session.py (agent loop)
                                          │
curl/外部 ──HTTP /api/chat──────────────▷─┘
                                          │
                                    ToolExecutor
                                     ├─ read_file / write_file / edit_file
                                     ├─ list_directory / search_code
                                     ├─ run_command
                                     ├─ jira_get_issue / jira_list_issues / jira_search
                                     ├─ jira_analyze  (拉取→脱敏→Claude CLI 分析)
                                     ├─ jira_fix      (分析→Claude CLI 修复→git push→MR)
                                     ├─ gitlab_list_mr_discussions
                                     ├─ gitlab_reply_discussion
                                     └─ gitlab_resolve_discussion

定时轮询 ──▷ 查询 open MRs ──▷ 筛选未处理评论 ──▷ agent loop ──▷ 自动回复/修复
```

单进程运行，所有组件通过 `python agent.py` 一次性启动。

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置

```bash
cp .env.example .env
```

编辑 `.env`，填入必要的配置：

| 变量 | 说明 | 必填 |
|------|------|------|
| `CLAUDE_API_KEY` | Claude API 密钥 | 是 |
| `CLAUDE_BASE_URL` | Claude API 地址 | 是 |
| `CLAUDE_MODEL` | 模型名称，默认 `claude-opus-4-6` | 否 |
| `PROJECT_DIR` / `OSBOT_PROJECT_DIR` | 工具操作的目标项目目录，默认 `$HOME` | 否 |
| `FEISHU_APP_ID` | 飞书应用 ID | 使用飞书时必填 |
| `FEISHU_APP_SECRET` | 飞书应用密钥 | 使用飞书时必填 |
| `FEISHU_CHAT_ID` | 限定响应的群聊 ID，留空则响应所有 | 否 |
| `BOT_NAME` / `FEISHU_BOT_NAME` | 机器人名称，默认 `Lutra` | 否 |
| `JIRA_SERVER` | JIRA 服务器地址 | 使用 JIRA 时必填 |
| `JIRA_PAT` | JIRA Personal Access Token | 使用 JIRA 时必填 |
| `JIRA_AEGIS_CAS` | SSO 认证 cookie，自动从 Chrome 读取，通常无需配置 | 否 |
| `MIMO_API_KEY` | Mimo 脱敏服务密钥 | 否 |
| `GITLAB_URL` | GitLab 地址，如 `https://git.n.xiaomi.com` | 使用 GitLab 时必填 |
| `GITLAB_PAT` | GitLab Personal Access Token | 使用 GitLab 时必填 |
| `GITLAB_PROJECT` | 项目路径如 `ai-framework/osbot`，留空自动从 git remote 检测 | 否 |
| `GITLAB_BOT_USERNAME` | Bot 的 GitLab 用户名，用于筛选自己提的 MR 和跳过自身评论 | 建议填 |
| `GITLAB_POLL_INTERVAL` | MR review 轮询间隔(秒)，`0` 不启用 | 否 |
| `GITLAB_POLL_CRON` | 固定轮询时间如 `09:00,14:00`，空不启用 | 否 |

GitLab 配置示例：

```bash
# GitLab 地址和认证
export GITLAB_URL="https://git.n.xiaomi.com"
export GITLAB_PAT="glpat-xxxxxxxxxxxxxxxxxxxx"   # Settings → Access Tokens → 勾选 api scope
export GITLAB_PROJECT=""                          # 留空自动从 git remote 检测，或填 "group/project"
export GITLAB_BOT_USERNAME="gongxi1"              # 你的 GitLab 用户名

# 轮询模式（二选一或同时启用）
export GITLAB_POLL_INTERVAL="300"                 # 每 300 秒轮询一次
export GITLAB_POLL_CRON="09:00,14:00"             # 每天 9 点和 14 点各轮询一次
```

### 3. 启动

```bash
source .env
python agent.py              # 默认端口 8901
python agent.py --port 9000  # 自定义端口
```

启动后输出：

```
  Lutra started
  Model       : claude-opus-4-6
  Work dir    : /home/user/workspace/project
  HTTP API    : http://0.0.0.0:8901
  Feishu      : chat_id=oc_xxx
  GitLab      : https://git.n.xiaomi.com (ai-framework/osbot)
  GitLab poll : every 300s, at 09:00,14:00
  Commands    : /reset /recall
  Press Ctrl+C to stop
```

## 使用方式

### 飞书

在群聊中 @Lutra 发送消息即可，机器人会显示 Typing 表情表示正在处理。

### HTTP API

不配置飞书时，可通过 HTTP 调试：

```bash
# 发送消息
curl -X POST http://localhost:8901/api/chat \
  -H "Content-Type: application/json" \
  -d '{"chat_id": "test", "sender_id": "user1", "text": "列出当前目录"}'

# 健康检查
curl http://localhost:8901/api/status
```

## 功能

### 基础工具

Lutra 能直接操作目标项目目录中的文件和命令：

- **读写文件** — `read_file`、`write_file`、`edit_file`
- **浏览目录** — `list_directory`
- **搜索代码** — `search_code`（正则匹配）
- **执行命令** — `run_command`（git、构建、测试等）

直接用自然语言描述需求即可，例如：

> 帮我看一下 src/main.py 里的 login 函数

> 把 config.yaml 里的 timeout 从 30 改成 60

> 运行一下单元测试

### JIRA 集成

配置 JIRA 后自动启用，支持以下操作：

| 说法 | 触发工具 | 作用 |
|------|----------|------|
| "列出我的 JIRA" | `jira_list_issues` | 列出分配给你的未解决 issue |
| "查看 OSBOT-32" | `jira_get_issue` | 获取 issue 详情 |
| "搜索 status=Open 的 issue" | `jira_search` | JQL 搜索 |
| "分析 OSBOT-32" | `jira_analyze` | 拉取 issue + 下载附件 + 脱敏 + Claude 深度分析 |
| "修复 OSBOT-32" | `jira_fix` | 分析（如未做）+ Claude 修复代码 + git push + 返回 MR 链接 |

#### 分析流程

```
jira_analyze("OSBOT-32")
  → 拉取 JIRA issue 详情
  → 下载附件到 data/jira/osbot-32/attachments/
  → Mimo 脱敏（可选）→ 写入 filtered_issue.txt
  → Claude CLI 分析根因、影响范围、修复方案
  → 写入 analysis.log
  → 返回分析报告
```

#### 修复流程

```
jira_fix("OSBOT-32")
  → 检查 analysis.log，不存在则先跑分析
  → git checkout -b fix/osbot-32
  → Claude CLI 实施最小改动修复
  → git add -A → git commit -s → git push -u origin
  → 返回 MR 链接
```

### GitLab MR Review 集成

配置 `GITLAB_PAT` 后自动启用，支持**手动查看**和**自动处理** MR review 评论。

#### 手动查看

飞书发消息即可触发：

| 说法 | 触发工具 | 作用 |
|------|----------|------|
| "查看 MR !123 的评论" | `gitlab_list_mr_discussions` | 列出 MR 上的 review 评论 |
| "回复 MR !123 的 discussion xxx" | `gitlab_reply_discussion` | 回复某条评论 |
| "resolve MR !123 的 discussion xxx" | `gitlab_resolve_discussion` | 标记评论已解决 |

#### 自动处理（定时轮询）

Lutra 定时查询自己提的 open MR，自动处理未回复的 review 评论。只需 PAT，无需项目管理员权限。

支持两种调度模式（可同时启用）：
- **固定间隔**：`GITLAB_POLL_INTERVAL=300` — 每 300 秒轮询一次
- **固定时间**：`GITLAB_POLL_CRON="09:00,14:00"` — 每天指定时间各执行一次

```
定时线程到达触发时间
  → 查询 bot 提的 open MRs
    → 遍历每个 MR
      → 获取 discussions
        → 筛选：unresolved + 最后一条 note 不是 bot 发的
          → agent loop：
            1. checkout 到 MR source_branch
            2. 读取相关代码，判断评论指出的问题是否存在
            3. 不存在 → 回复解释理由
            4. 存在 → 修复代码 + git push + 回复修复方案
            5. 标记评论为 resolved
          → 飞书通知处理结果（可选）
```

去重逻辑：只处理"未 resolved 且最后一条 note 不是 bot 发的"discussion，bot 回复后自然跳过。

#### GitLab PAT 权限

创建 Personal Access Token 时勾选以下 scope：

- `api` — 读写 MR discussions、回复评论、resolve

#### 验证

```bash
# 1. 设置轮询间隔，启动后日志每 60s 出现 "[POLL] checking N open MRs..."
export GITLAB_POLL_INTERVAL=60
source .env && python agent.py

# 2. 飞书发送 "查看 MR !1 的评论" 测试手动查看

# 3. 在 MR 上留评论 → 等轮询触发 → GitLab 上出现 bot 回复

# 4. 也可设置固定时间轮询
export GITLAB_POLL_CRON="09:00,14:00"
```

### 会话命令

| 命令 | 作用 |
|------|------|
| `/reset` 或 `/重置` | 清空当前会话，生成摘要存入记忆 |
| `/recall` 或 `/回忆` | 查看历史记忆 |

### 上下文管理

- 会话消息超过 150K tokens 时自动压缩（保留最近 30 条）
- 会话每 5 条消息或 60 秒自动持久化到 SQLite
- 新会话自动注入相关历史记忆
- 会话 1 小时无活动自动过期

## 项目结构

```
agent.py                  # 入口：启动飞书 WS + HTTP API + GitLab 轮询 + 后台清理
lutra/
├── config.py             # pydantic-settings 配置，读取 .env
├── llm.py                # Claude API 封装（chat + summarize）
├── models.py             # SessionState、Memory 数据模型
├── session.py            # agent loop + 会话生命周期
├── tools.py              # 工具定义 + ToolExecutor
├── feishu.py             # 飞书 WebSocket 收发
├── context.py            # token 追踪 + 上下文压缩
├── jira_client.py        # JIRA API 封装（获取/搜索/下载附件）
├── gitlab_client.py      # GitLab REST API 封装（MR 轮询 + discussions）
├── sensitive_filter.py   # Mimo 脱敏
└── memory/
    ├── store.py          # SQLite 存储（记忆/会话/角色）
    └── retrieval.py      # 基于关键词的记忆检索
data/
├── lutra.db            # SQLite 数据库（WAL 模式）
└── jira/                 # JIRA 分析/修复产物
    └── osbot-32/
        ├── attachments/
        ├── filtered_issue.txt
        ├── analysis.log
        └── fix.log
```
