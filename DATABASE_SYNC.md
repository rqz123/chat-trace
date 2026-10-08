# 聊迹数据库在两台电脑间交接

电脑 A、B 各自在本地 `chat-trace` 项目目录运行聊迹，通过 Google Drive 的 `shared-state` 目录传递经过完整性检查的 SQLite 快照。实际运行的数据库始终是本机的 `data\chattrace.db`，不会直接在 Google Drive 上运行 SQLite。

只交接归档数据库 `chattrace.db`（会话选择、已归档消息、同步游标、话题总结）。以下内容**不会**同步，每台电脑各自保存：

- `data\wechat-cache\`：微信数据库密钥（DPAPI 加密，只有本机当前 Windows 用户能解密）和临时解密副本；每台电脑首次同步时自动从本机微信获取；
- `data\openai_config.json`：OpenAI 配置；
- `.venv`、日志、本地备份。

## 使用原则

- 任何时候只能有一台电脑取得控制权并运行聊迹。Google Drive 同步有延迟，租约文件只能防止日常误操作，不能代替真正的分布式锁。切换前务必等待 Google Drive 显示 **Up to date（同步完成）**。
- 两台电脑应运行同一版本的代码。快照记录了数据库结构版本；如果快照比本机代码新，`take.cmd` 会拒绝恢复并提示先 `git pull` 和 `pip install -e .`。
- 每台电脑归档的是**本机微信**里的消息。消息以微信服务器消息 ID 识别，所以同一条消息在 A、B 上各同步一次也只保存一条。

## 日常切换

在当前使用聊迹的电脑上释放控制权（会停止本机服务并发布快照）：

```powershell
.\release.cmd
```

等待 Google Drive 显示 **Up to date**，然后在另一台电脑上取得控制权（会恢复快照并启动服务）：

```powershell
.\take.cmd
```

随时查看状态：

```powershell
.\sync-status.cmd
```

正在同步、「启动微信并归档」或生成话题总结时，`release.cmd` 会拒绝停止服务，请等它完成后再试。

交换目录默认为 `G:\My Drive\Claude\Project\chat-trace\shared-state`。如果某台电脑的 Google Drive 盘符不同，可设置环境变量 `CHATTRACE_SHARED_DIR`，或在命令中指定：

```powershell
.\take.cmd -SharedDir "H:\My Drive\Claude\Project\chat-trace\shared-state"
```

## 第一次使用

1. 两台电脑都更新到包含这些脚本的代码，并按 README 安装 `.venv`。
2. 聊迹的数据目录必须是项目下的 `data\`（`start.cmd` 固定使用它）。如果某台电脑之前用的是默认的 `%LOCALAPPDATA%\ChatTrace`，先停止聊迹，再把其中的 `chattrace.db` 复制到项目的 `data\`。
3. 在归档内容最新的电脑上运行 `.\release.cmd`，发布第 1 代快照。
4. 等待 Google Drive 同步完成，在另一台电脑上运行 `.\take.cmd`。

`take.cmd` 会用快照替换本机数据库。如果两台电脑上的归档都有对方没有的内容，被替换的一方仍保存在 `data\sync-local-backups` 中；取得控制权后同步一次微信，可以补回本机微信里还有的近期消息。

## 恢复与备份

- 如果持有控制权的电脑异常关机，先确认那台电脑的聊迹已停止，等待 Google Drive 同步完成，再运行 `.\take.cmd -Force`。
- 每次发布的历史快照都保留在 `shared-state\snapshots`，可按需手动清理旧快照。
- 快照替换本机数据库前，旧数据库会备份到 `data\sync-local-backups`。
- 出现哈希、文件大小或数据库完整性错误时，等待 Google Drive 同步完成后重试。`-Force` 不会绕过这些检查。
