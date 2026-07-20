# 使用、迁移与故障排查

## 首次启动

1. 复制完整 `ews_sg` 文件夹，不要只复制 `.command` 文件。
2. 双击 `run_backup_app.command`。
3. 第一次运行会创建 `.venv` 并安装依赖，耗时取决于网络。
4. 页面打开后填写站点 origin、Script Name、API Key、可选代理和输出目录。
5. 运行完整检查，通过后开始备份。

启动器的位置解析全部使用相对路径，因此文件夹可以放在桌面、外接盘或名字含空格的目录。`.venv` 是目标电脑本地环境，不需要随文件夹复制；复制时可删除 `.venv` 和 `.local` 来减小体积。

## Python 与依赖

要求 Python 3.9+。依赖固定在 `requirements.txt`。启动器会优先直接安装；若安装依赖本身也必须走代理，可在启动前设置 `SHOTGRID_BOOTSTRAP_PROXY=host:port`。代理不会关闭 TLS 校验。

若启动器提示 Python 版本不足，请从 Python 官方安装通用 macOS 包，再重新双击。若复制来的 `.venv` 不能在本机运行，启动器会把它移到 `.local/runtime/foreign_venv_<time>` 后自动重建。若 macOS 阻止执行，可在 Finder 中右键 `.command` 选择“打开”；若执行位在传输中丢失，可运行 `chmod +x run_backup_app.command`，不要关闭 Gatekeeper。

## ShotGrid 凭据

在 ShotGrid 管理界面创建 Script/API User，授予读取全部待备份实体和字段的权限。完整备份范围受该 Script 权限约束：Script 看不到的实体、字段和项目无法由工具绕过权限获取。

推荐使用专用只读 Script。不要复用管理员个人密码。API Key 一旦发到聊天、工单或日志，应在测试后轮换。

## 代理

UI 只接受 `host:port`，不接受 `http://`、URL 路径或 `user:password@host`。需要认证代理时，不要把密码写进 UI 或仓库；应由系统或受控环境配置处理。

连接失败时依次确认：

1. 本地代理进程确实监听该端口。
2. ShotGrid 地址只含 `https://host`，没有项目路径、query 或 fragment。
3. Script Name 与 Key 属于同一个站点。
4. Script 至少能读取 `Project` 与 schema。

## 输出目录与空间

默认输出在项目内 `.local/backups`，Git 会忽略它。正式长期备份建议选择加密外接盘，并至少保留两份、两种介质、其中一份异地。

备份图片、filmstrip、Version 上传媒体与 Attachment 原文件可能远大于记录 JSON。预检查只能确认当前可用空间，不能从所有临时下载 URL准确预测最终大小。运行中磁盘不足会产生 `.incomplete`，不会发布成完整快照。

## 日志怎么看

- UI 日志：给当前操作者看，只保留最近事件。
- `logs/events.jsonl`：快照内结构化事件，含递增 `seq`、UTC 时间、阶段、实体、分页和下载结果，不含业务记录正文。
- `logs/errors.json`：最终错误数组；完整快照应为空数组。
- `manifest.json`：恢复和审计的主要事实源，包含 scope、实体计数、文件路径、hash、工具版本和媒体策略。
- `COMPLETED.json`：发布回执，锁定 manifest 与 checksum 清单的 SHA-256。

不要把 `.incomplete` 目录手工改名为正式快照。排障后应重新运行一次完整备份。

## 跨电脑验证

复制完成后，在新电脑安装依赖并运行：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python tools/shotgrid_backup/snapshot_verify.py /path/to/snapshot --verify --require-full
```

返回 `"ok": true` 才表示副本在文件层面完整。`latest.txt` 只是便捷指针，恢复或校验不依赖它。

## 常见状态

- “检查失败”：没有启动备份；修改凭据、权限、代理或输出目录后重新检查。
- “已有备份正在运行”：同一应用或输出目录有任务；等待结束。若电脑异常关机留下 `.backup.lock`，先确认没有 `backup.py` 进程，再手工删除该锁。
- `.incomplete`：至少一个实体或文件失败；看 `manifest.json`、`logs/errors.json` 和事件尾部。
- `partial`：绝不能作为完整备份交付或更新 `latest.txt`。
- 校验 SHA 不匹配：副本损坏或被修改，从另一份备份重新复制，不要覆盖唯一原件。
