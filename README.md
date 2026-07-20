# ShotGrid 本地完整备份

这是一个面向 macOS 的本地 ShotGrid 备份工具。图形应用固定执行完整备份：自动发现当前 Script 凭据可读的全部实体，保存 active / retired 记录、归档项目、schema、Attachment 原文件，以及 API 返回的可下载图片、filmstrip 和上传媒体。界面不提供实体、增量或附件开关，避免误操作造成不完整快照。

本工具只读取 ShotGrid，不提供恢复按钮，也不会执行 create、update、delete、retire 或上传。快照格式、日志和完整性校验旨在为未来独立的逻辑恢复工作保留当前 API 能采集的输入。

## 在 Mac 上使用

把整个 `ews_sg/` 文件夹复制到目标 Mac，双击：

```text
run_backup_app.command
```

启动器会从自身位置定位项目，创建私有 `.venv`，安装锁定版本 `shotgun_api3==3.10.2`，然后打开只监听 `127.0.0.1` 的本地页面。项目不依赖 NEWG 路径。目标电脑需要 Python 3.9+，首次安装依赖还需要能访问 PyPI、受控镜像或已配置的 bootstrap 代理；缺少 Python 时启动器会停止并打开 Python 官方下载页。

页面只需要填写：

- ShotGrid origin，例如 `https://your-site.shotgrid.autodesk.com`。
- Script Name 与 API Key。
- 可选 HTTP 代理，格式为 `host:port`，例如 `127.0.0.1:7892`。
- 本地输出目录；默认 `.local/backups`。

先点“运行完整检查”。检查会真实鉴权、发现全部可读实体、逐实体验证字段读取、统计 active / retired 数量、检查输出权限与磁盘空间，并自动选择不超过 8 个 worker。通过后才能开始备份。API Key 在检查后立即从输入框清除，启动任务使用 10 分钟有效的一次性内存 handle；本工具不主动把 Key 写入配置、日志或快照。

## 完整快照的判定

只有下列条件全部成功，临时目录才会原子发布为正式快照并更新 `latest.txt`：

- 所有计划实体的 schema 与记录文件导出成功。
- 支持 retired 的实体已同时导出 retired 记录。
- 所有 `Attachment.this_file` upload 原文件下载成功。
- 所有 API 可直接下载的 image、filmstrip 和 upload URL 媒体下载成功。
- 每个 payload 文件已写入 `checksums.sha256`。
- `manifest.json`、checksum 清单和 `COMPLETED.json` 发布回执相互匹配。
- 没有未处理错误。

任一可捕获的实体或文件错误都会让目录保留为 `<snapshot_id>.incomplete`，`manifest.json` 状态为 `partial`，并且不会更新 `latest.txt`。断电、强制结束进程或磁盘硬故障也只会留下 `.incomplete`，但可能来不及生成最终 manifest。任何 `.incomplete` 都不能作为完整快照。因此 UI 的“备份完成”只表示这次 API 可见范围内的快照通过了完成门禁。

## 输出结构

```text
<output>/
  latest.txt
  <snapshot_id>/
    COMPLETED.json
    manifest.json
    checksums.sha256
    schema/
      entities.json
      fields/<EntityType>.json
    entities/<EntityType>.jsonl
    links/<EntityType>.jsonl
    attachments/
      index.json
      <attachment files>
    media/
      index.json
      <EntityType>/<media files>
    logs/
      events.jsonl
      errors.json
```

实体 JSONL 使用稳定 envelope：`source.type + source.id` 保存来源身份，`state` 保存 active / retired 状态，`record` 保存原 API 字段。`links/*.jsonl` 额外展开所有 entity / multi_entity 关系，并保留 multi_entity 的 ordinal；connection 实体本身也在实体文件中。所有文件索引都使用快照内相对路径，复制到另一台电脑或其他磁盘后仍能校验。

快照目录在 macOS / Linux 上使用 `0700`，文件使用 `0600`。备份仍可能包含人员、项目、签名媒体 URL 等敏感信息，应放在 FileVault 或其他加密磁盘中，并制定独立保留策略；不要提交 Git。

## 只读校验

校验不会连接或写入 ShotGrid：

```bash
.venv/bin/python tools/shotgrid_backup/snapshot_verify.py \
  /path/to/snapshot --verify --require-full
```

校验会检查 full-site profile、readable/planned/exported 实体集合、空错误日志、完成回执、全部 SHA-256、JSONL 格式、记录/关系数量、source envelope、附件和媒体大小与哈希，以及未登记文件。把快照复制到新电脑后，应先运行此命令再认定副本可用。

## CLI（高级用途）

图形应用始终全量。CLI 为自动化和诊断保留显式选项：

```bash
export SHOTGRID_URL="https://your-site.shotgrid.autodesk.com"
export SHOTGRID_SCRIPT_NAME="local_backup"
export SHOTGRID_SCRIPT_KEY="..."
export SHOTGRID_HTTP_PROXY="127.0.0.1:7892"  # 可选

.venv/bin/python tools/shotgrid_backup/backup.py --check
.venv/bin/python tools/shotgrid_backup/backup.py --all-readable --workers 8
```

CLI 不带 `--all-readable` 时保留历史上的核心实体默认集；不要把这种快照称为完整站点备份。`--entities` 与 `--updated-since` 只用于高级诊断或增量实验，不能替代周期性完整快照。

只探察 schema、实体计数和媒体字段覆盖而不导出业务记录值：

```bash
.venv/bin/python tools/shotgrid_backup/audit.py --http-proxy 127.0.0.1:7892
```

报告默认写入 Git 忽略的 `.local/audits/latest.json`；它仍包含站点结构与数量信息，应按内部诊断数据处理。

## 已知边界

- ShotGrid API 不提供跨实体数据库事务。全量模式使用稳定 ID keyset 扫描，增量模式用时间上界；运行中新增、更新或删除仍可能形成极小的一致性窗口，应在业务低峰执行并保留多代快照。
- API 已硬删除且不可见的数据无法事后备份。
- `PublishedFile` 和 `LocalStorage` 中的路径会完整保存为元数据，但它们指向的 NAS、本地盘或云盘文件不属于 ShotGrid API 文件内容。外部存储必须另行备份。因此本工具旨在保留未来逻辑恢复所需的 ShotGrid API 输入，但尚不等同于经过目标站演练的完整灾难恢复方案。
- ShotGrid 派生转码不一定能从 API 取回；manifest 会把不可下载 URL 计为 `media.metadata_only`，不会把它冒充成本地文件。
- schema 证明源站当时有哪些字段，但未来逻辑恢复仍需在目标站处理 custom field、系统实体、来源 ID 到目标 ID 映射与链接循环。

完整的快照合同、日志字段、恢复顺序和人工边界见 [docs/snapshot_restore_contract.md](docs/snapshot_restore_contract.md)，安装与故障排查见 [docs/user_guide.md](docs/user_guide.md)。

## 作者

Wangbo
