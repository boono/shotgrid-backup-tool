# 使用、迁移与故障排查

## 首次启动

1. 复制完整 `ews_sg` 文件夹，不要只复制 `.command` 文件。
2. 双击 `run_backup_app.command`。
3. 第一次运行会创建 `.venv` 并安装依赖，耗时取决于网络。
4. 页面打开后填写站点 origin、Script Name、API Key、可选代理和输出目录。
5. 运行完整检查，通过后开始备份。

启动器的位置解析使用相对路径，因此文件夹可以放在桌面、外接盘或名字含空格的目录。`.venv` 是目标电脑本地环境，不需要随工具复制；复制工具时可以删除 `.venv` 和 `.local` 来减小体积。

## 已有实体快照时如何补媒体

如果另一台电脑已经有完整实体快照，复制时保留输出根的 `latest.txt` 和它指向的完整 base 目录。在新电脑的页面里选择这个输出根，而不是只选择 base 目录。

应用会读取 `<output>/latest.txt`，校验其指向的已完成 base，并以该 base 的实体记录作为媒体清单。它不会重新拉取全部实体，也不会修改 base；每次补全都在以下位置创建新的不可变目录：

```text
<output>/media_supplements/<base_snapshot_id>/<supplement_id>/
```

失败或中断的运行保留为 `<supplement_id>.incomplete`。不要删除后缀或把它手工改成正式包；排障后重启会优先校验并续用这份 `.incomplete`，已经完成且 hash 正确的文件直接跳过，只处理失败、损坏和缺失项。若旧暂存包的 lineage 或策略不再匹配，才创建新的 supplement ID。已经完成的 supplement 不会原位更新。

单个下载失败时会先执行有上限的自动重试；过期的 ShotGrid locator 会按实体 ID 和字段重新查询。仍然失败的项目会以脱敏错误码写入 supplement 的 manifest、`logs/errors.json`、`logs/events.jsonl` 和断点索引。只要一个 required ShotGrid 托管媒体最终失败，本轮就不生成 `COMPLETED.json`，UI 明确显示失败数量。重新启动后不会从零开始：程序逐项验证现有文件的 SHA-256，正确项跳过，失败、损坏和缺失项重新处理。没有可信旧 index/hash 的文件不计为已完成。

如果 `latest.txt` 不存在、指向不存在的目录，或指向的 base 没有通过完成门禁，应用不会把它当作可复用实体快照。没有可复用 base 时，新的完整备份会先生成并发布 base，然后自动开始媒体补全。

## 迁移旧版媒体阶段中断的任务

旧版任务可能已经完成全部实体导出，却在媒体阶段被用户停止，留下一个 `.incomplete` 目录和上千个已经下载的文件。新版不会把整个 `.incomplete` 直接当成完整快照，也不会要求无条件重拉实体，而是执行受约束的 salvage：

1. 自动核对旧目录中的 `entity_complete` 证据、实体/schema/关系计数、hash 和错误状态。
2. 只有实体层可以被独立证明完整时，才从这些不可变内容重建新的 sealed base 数据基线；旧 `.incomplete` 不改名、不覆盖、不删除。
3. 对旧目录中已经完成且能验证大小/hash/来源的媒体，在 macOS/APFS 支持时使用 copy-on-write clone，否则创建独立的校验复制，避免重新下载，同时不让旧暂存目录与新包共享可变 inode。
4. 新 supplement 重新建立 lineage、manifest 与 checksum，只下载或复制仍然缺失的 required media。
5. 任何旧文件不能证明完成、来源不匹配或 hash 不符时，不纳入已完成集合，按缺失项重新获取。

如果无法核对 `entity_complete` 计数、实体/关系文件、实体范围或 `source.site`，应用必须拒绝自动 salvage；不能因为目录里“看起来已经有很多 JSON 或媒体”就把它提升为完整 base，也不能把当前登录站点强行写成旧数据来源。真正的 schema-v1 包没有这些 v3 证据，因此会重新导出 v3 entity base；只有 v1 manifest 明确属于同一站点且旧媒体 index/hash 可验证时，已下载媒体才会被复用。原中断目录会原样保留。

## Python 与依赖

要求 Python 3.9+。依赖固定在 `requirements.txt`。启动器会优先直接安装；若安装依赖本身也必须走代理，可在启动前设置 `SHOTGRID_BOOTSTRAP_PROXY=host:port`。代理不会关闭 TLS 校验。

若启动器提示 Python 版本不足，请从 Python 官方安装通用 macOS 包，再重新双击。若复制来的 `.venv` 不能在本机运行，启动器会把它移到 `.local/runtime/foreign_venv_<time>` 后自动重建。若 macOS 阻止执行，可在 Finder 中右键 `.command` 选择“打开”；若执行位在传输中丢失，可运行 `chmod +x run_backup_app.command`，不要关闭 Gatekeeper。

## ShotGrid 凭据

在 ShotGrid 管理界面创建 Script/API User，授予读取全部待备份实体、字段和托管媒体的权限。完整范围受该 Script 权限约束：Script 看不到的实体、字段、项目或媒体无法由工具绕过权限获取。

推荐使用专用只读 Script，不要复用管理员个人密码。API Key 一旦发到聊天、工单或日志，应在测试后轮换。工具不需要、也不会保存 NAS、文件服务器或云存储凭据。

## 代理

UI 只接受 `host:port`，不接受 `http://`、URL 路径或 `user:password@host`。需要认证代理时，不要把密码写进 UI 或仓库；应由系统或受控环境配置处理。

连接失败时依次确认：

1. 本地代理进程确实监听该端口。
2. ShotGrid 地址只含 `https://host`，没有项目路径、query 或 fragment。
3. Script Name 与 Key 属于同一个站点。
4. Script 至少能读取 `Project`、schema 和目标媒体。

## 媒体来源与外部存储

补全阶段会下载 ShotGrid 托管的 upload、Attachment、image 和 filmstrip。PublishedFile 另按上下文处理：

- 外部 PublishedFile / 本地 / NAS Copy 默认关闭；只有用户勾选后，`PublishedFile.path` 或 `link_type=local` 才进入复制范围。此时路径必须在当前电脑已经挂载并可读，否则属于本轮 required media 失败。
- 工具不会自动 mount NAS 或本地卷，也不会提示、存储或转发外部存储凭据。应由操作者在启动任务前通过操作系统完成挂载和授权。
- 路径显式包含 `%04d`、`%d`、`####`、`@@@@`、`$F4` 或 `$F` 时才按序列展开。目录中即使有大量相似文件，没有显式 token 也不会被自动猜成序列。
- PublishedFile 上明确属于媒体的 HTTPS 地址走隔离下载流程。普通业务 web URL 仍只保存为实体元数据，不抓取页面，也不跟随其附件链接。

事后补全 image 时，原有临时下载地址可能已经失效，工具可能需要重新向 ShotGrid 获取当前下载入口。该结果可能是补全时刻的当前媒体，而不是 base 时刻的历史原件；manifest 与日志会把这种来源标记为 `current_refetch`，不要把它当作历史时点 `exact`。

## 文件布局与大量序列

supplement 不把全站文件堆在单个目录中。媒体统一按 entity → ID 分片 → record ID → field 分层，数量较大的序列再按 frame 分片：

```text
media/
  <EntityType>/
    <id_shard>/
      <record_id>/
        <field>/
          <single_file>
          <frame_shard>/
            <sequence_frames>
```

Attachment 也进入以 `Attachment` entity、attachment ID 与 `this_file` field 定位的分层，不建立全站平铺的 `attachments/` 大目录。任何全站 `media/` 或 `attachments/` 目录都不得在同一级平铺几万份文件。实际分片名、原始来源、sequence token、frame 范围、文件大小和 SHA-256 以 supplement manifest 为准。

## 带宽、并发与进度

网络 download 与本地/NAS copy 使用不同资源池，均从低并发开始。调度器根据实际吞吐、错误率和重试动态升降并发，避免网络下载和磁盘复制互相拖死。这个策略没有用户选项，不需要手工设置 worker 或带宽。

UI 会持续打印已完成与总量、字节进度和 ETA，并分别显示 ShotGrid 下载的当前带宽/并发和本地/NAS 复制的当前带宽/并发，不把两类工作合并成一个速率。download 与 copy 的 ETA 分别使用各自最近 100 个已完成任务耗时的滑动平均，再按各自当前并发计算；样本少于 10 个时显示“校准中”，不使用全程累计平均。ETA 会随网络、NAS、并发和文件大小变化，只是动态估计，不是完成承诺。速度调低不会跳过 required media；最终仍以 manifest、checksum 和完成回执为准。

## 输出目录与空间

默认输出在项目内 `.local/backups`，Git 会忽略它。正式长期备份建议选择加密外接盘，并至少保留两份、两种介质、其中一份异地。

upload、Attachment、image、filmstrip、PublishedFile 与序列可能远大于实体 JSON。预检查只能确认当前可用空间，不能精确预测临时下载、序列展开和远程存储的最终大小。媒体阶段磁盘不足会产生 `.incomplete` supplement，不会修改已完成 base。

## 日志、manifest 与完成回执

- base `logs/events.jsonl` 与 `logs/errors.json`：实体快照运行的结构化事件和错误。
- supplement `logs/`：媒体发现、下载、复制、重试、调速、`current_refetch` 和失败原因。
- supplement `manifest.json`：媒体来源、required 状态、文件相对路径、大小、hash、序列展开和 base lineage 的主要事实源。
- supplement `checksums.sha256`：覆盖补全包 payload。
- supplement `COMPLETED.json`：锁定 supplement manifest 与 checksum 清单；`.incomplete` 不得被视为有有效完成回执。

lineage 必须把 supplement 绑定到 base snapshot ID 及其关键清单 SHA-256。base 完成、实体数量正确，只能证明实体层；只有同一 lineage 下 supplement 也完成且 checksum 匹配，才可声明该次 required media 补全完成。

## 跨电脑复制与验证

跨电脑时必须一起复制：

```text
<output>/<base_snapshot_id>/
<output>/media_supplements/<base_snapshot_id>/
```

建议同时保留输出根 `latest.txt`，方便应用自动发现，但它只是指针。只复制 base 会丢媒体，只复制 supplement 会丢实体与 lineage 所依赖的事实源；任一情况都不能称为完整媒体备份。

实体 base 可在新电脑运行只读文件校验：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python tools/shotgrid_backup/snapshot_verify.py /path/to/base --verify --require-full
```

返回 `"ok": true` 只说明 base 通过实体快照校验。还应核对 supplement 目录不是 `.incomplete`、`COMPLETED.json` 与 manifest/checksum 相互匹配、lineage 指向这个 base，且所有 payload SHA-256 正确。

可以用一个命令完成 base 与 supplement 的组合校验：

```bash
.venv/bin/python tools/shotgrid_backup/snapshot_verify.py \
  /path/to/base --verify --require-full \
  --media-supplement /path/to/media_supplement --require-all-media
```

如果运行时关闭了可选的 PublishedFile / 本地 / NAS Copy，supplement 会明确标记 external media 为 `not_requested`；这时 ShotGrid 托管媒体仍可完整，但 `--require-all-media` 会按“外部媒体也必须覆盖”的严格口径返回失败。

## 常见状态

- “检查失败”：没有启动备份；修改凭据、权限、代理、输出目录或挂载状态后重新检查。
- “已有备份正在运行”：同一应用或输出目录有任务；等待结束。若电脑异常关机留下锁文件，先确认没有相关备份进程，再按页面提示处理，避免同时写入同一目标。
- base `<snapshot_id>.incomplete`：实体或 base 文件失败，不能补媒体，也不能更新 `latest.txt`。
- supplement `<supplement_id>.incomplete`：至少一个 required media 无法访问、下载、复制或校验；看 supplement manifest、错误日志和事件尾部。
- `current_refetch`：取得的是补全时刻的当前 image 候选，不是历史时点 exact 证明。
- 校验 SHA 不匹配：副本损坏或被修改，从另一份备份重新复制，不要覆盖唯一原件。
- PublishedFile 路径缺失：先在当前机器正确挂载对应盘或 NAS 并确认读取权限，再创建新的 supplement；工具不会替你 mount 或保存凭据。

当前工具没有恢复功能。不要把“base 与 supplement 均完整”误解为已经在目标 ShotGrid 完成恢复演练。

## 作者

Wangbo
