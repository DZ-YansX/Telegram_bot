# Telegram Claude Bot

一个运行在 AWS Lambda 上的 Telegram 机器人，用 Claude 回复私聊和群聊消息。

功能：多模型切换与思考开关、图片和贴纸识别、群聊中 @ 或回复触发、长回复分条发送、滚动记忆、每日定时群消息。

命令：`/model` `/memory` `/clear` `/status` `/chatid` `/help`

## 架构

```
Telegram → Function URL → Webhook Lambda → Worker Lambda → Claude → Telegram
                                 └────── DynamoDB ──────┘
```

- **Webhook**（`lambda_function.lambda_handler`）：校验 secret 和聊天白名单，保存 Update，异步调用 Worker，立即返回 200。
- **Worker**（`lambda_function.worker_handler`）：调用 Claude、发送回复、写回状态。
- **DynamoDB**：保存对话历史、记忆、模型设置、Update 去重记录和聊天锁。
- **EventBridge Scheduler**：每天发送一条定时群消息。

两个函数共用一个 ZIP，不需要 API Gateway、SQS 或 VPC。

| 文件 | 用途 |
| --- | --- |
| `bot.py` | 消息处理、提示词、命令；也可以本地 polling 运行 |
| `lambda_function.py` | 两个 Lambda 入口 |
| `storage.py` | DynamoDB 存储 |
| `deploy.py` | 构建、部署、设置 Webhook |

## 准备

- Python 3.12+，以及能访问 AWS 的凭证（例如 `aws login`）
- 从 [@BotFather](https://t.me/BotFather) 获取的 Bot token
- [Anthropic API key](https://console.anthropic.com/)

在项目根目录创建 `secrets.json`（已加入 `.gitignore`）：

```json
{
  "TELEGRAM_BOT_TOKEN": "123456:ABC...",
  "ANTHROPIC_API_KEY": "sk-ant-...",
  "ALLOWED_CHAT_IDS": "123456789,-1001234567890",
  "GROUP_CHAT_ID": "-1001234567890",
  "PERSONA_PROFILE": "- 大学生，喜欢写代码和打游戏"
}
```

- `ALLOWED_CHAT_IDS`：允许使用机器人的聊天 ID，用逗号分隔；不在列表里的聊天会被拒绝。可以先部署，再在聊天中发 `/chatid` 查看 ID。
- `GROUP_CHAT_ID`：每日定时消息发往的群。
- `PERSONA_PROFILE`：可选，关于你本人的设定（身份、爱好等），会作为「关于哥哥」加进人设，避免把个人信息写进源码。
- `TELEGRAM_WEBHOOK_SECRET`：可以不填，首次部署时会自动生成并写回这个文件。

## 部署

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt "boto3[crt]"

.venv/bin/python deploy.py build     # 打包 Linux ARM64 的 ZIP 到 build/
.venv/bin/python deploy.py deploy    # 创建/更新 Lambda、DynamoDB、IAM、Scheduler
.venv/bin/python deploy.py webhook   # 把 Function URL 注册为 Telegram Webhook
.venv/bin/python deploy.py status    # 查看 Webhook 状态
```

默认地区是 `ap-southeast-1`，可以通过 `AWS_REGION` 修改。部署结果（资源名、Function URL）会写入 `deployment.json`。

每日定时任务首次创建时是**禁用**的，需要的话到 AWS Console → EventBridge Scheduler 里启用 `telegram-claude-daily`。

修改代码后，重新执行 `build` 和 `deploy` 即可。

## 日志

```sh
aws logs tail /aws/lambda/telegram-claude-worker --since 30m
```

## 本地运行

本地用 polling 模式运行，状态存在 SQLite 中。需要先删除 Webhook，否则 Telegram 会拒绝 polling：

```sh
.venv/bin/python deploy.py delete-webhook
.venv/bin/pip install "python-telegram-bot[job-queue]==22.8"
set -a; eval "$(python3 -c "import json,shlex;print('\n'.join(f'{k}={shlex.quote(v)}' for k,v in json.load(open('secrets.json')).items()))")"; set +a
.venv/bin/python bot.py --polling
```

结束后执行 `deploy.py webhook` 切回 Lambda。用 `deploy.py export-db` / `import-db` 可以在 DynamoDB 和本地 SQLite 之间迁移数据。

## 费用

在常规个人使用量下，Lambda、DynamoDB（预置 5 RCU/WCU）、Scheduler 和 CloudWatch Logs 基本都在 AWS 长期免费额度内。Claude API 单独按用量计费。
