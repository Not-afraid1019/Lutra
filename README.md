# Lutra

本地 AI 编码助手 —— 通过飞书接收指令，使用 Claude 读写代码、执行命令、分析和修复 JIRA issue。

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
                                     └─ jira_fix      (分析→Claude CLI 修复→git push→MR)
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
agent.py                  # 入口：启动飞书 WS + HTTP API + 后台清理
lutra/
├── config.py             # pydantic-settings 配置，读取 .env
├── llm.py                # Claude API 封装（chat + summarize）
├── models.py             # SessionState、Memory 数据模型
├── session.py            # agent loop + 会话生命周期
├── tools.py              # 工具定义 + ToolExecutor
├── feishu.py             # 飞书 WebSocket 收发
├── context.py            # token 追踪 + 上下文压缩
├── jira_client.py        # JIRA API 封装（获取/搜索/下载附件）
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
