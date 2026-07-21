# ShotGrid 本地完整备份与媒体补全

这是一个面向 macOS 的本地 ShotGrid 备份工具。图形应用固定执行完整工作流：先生成可读实体、active / retired 记录、归档项目、schema 和关系索引组成的不可变实体快照（base），再自动为该 base 生成不可变媒体补全包（media supplement）。

如果另一台机器已经生成了完整实体快照，把该输出目录连同 `latest.txt` 复制到当前机器并在页面中选中它即可。新版会从所选 `<output>/latest.txt` 自动发现 base，不重新拉取实体，也不修改原快照，而是在 `media_supplements/<base_snapshot_id>/<supplement_id>/` 新建补全包。没有可复用 base 时，新备份会先发布 base，再自动进入媒体补全阶段。

本工具只读取 ShotGrid，并按 base 中已经保存的路径读取当前机器可访问的本地盘或 NAS 文件。它不提供恢复按钮，也不会对 ShotGrid 执行 create、update、delete、retire 或上传。快照合同只为未来独立的逻辑恢复或迁移工具保存输入，不代表恢复功能已经存在。

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
- 本地输出目录；默认 `.local/backups`。已有 base 时应选择包含 `latest.txt` 的同一个输出目录。
- 可选的 PublishedFile / 本地 / NAS Copy；默认关闭，不影响 ShotGrid 托管媒体全量下载。

先点“运行完整检查”。检查会真实鉴权、发现当前 Script 可读范围、检查输出权限与磁盘空间，并识别 `latest.txt` 指向的已完成 base。可复用 base 不会再次导出实体；没有可复用 base 时才执行实体备份。API Key 在检查后立即从输入框清除，启动任务使用 10 分钟有效的一次性内存 handle；本工具不主动把 Key 写入配置、日志、base 或 supplement。

## Base 与媒体补全的完成判定

实体 base 和媒体 supplement 各自不可变、各自发布完成回执：

- base 必须完整导出计划内 schema、active / retired 记录、关系索引和日志，通过 `checksums.sha256` 校验，并具有匹配的 `manifest.json` 与 `COMPLETED.json`。
- supplement 必须保存与 base 的 lineage，完成所有 required media 的下载或复制，通过自己的 `checksums.sha256`，并具有匹配的 `manifest.json` 与 `COMPLETED.json`。
- supplement 不会向 base 回写文件、索引或状态；重启会续用 lineage/策略匹配的 `.incomplete` 断点，只有不匹配时才创建新的 supplement ID，绝不覆盖已经完成的包。

任何 required media 无法访问、下载、复制或校验时，本次 supplement 都保留为 `<supplement_id>.incomplete`，不得生成有效的 `COMPLETED.json`，也不得宣称“全媒体完成”。原 base 仍保持其原有完成状态，但 base 完整不等于媒体补全成功。

旧版任务若在实体导出结束、媒体阶段中断，可能留下含大量已下载文件的旧 `.incomplete`。新版只有在自动核对 `entity_complete` 计数、实体/关系文件和 source.site 后才允许 salvage：从可验证的实体内容建立恢复基线，通过 APFS clone（可用时）或独立校验复制复用已完成媒体，再只补缺失项。缺少来源证据、声明了其他站点或无法证明实体范围时必须拒绝自动封存，绝不把当前登录站点强行贴到旧数据上。真正的 schema-v1 包不会被升级为 v3 entity base；工具会先重新生成 v3 base，之后只在 v1 manifest 能证明同站点且旧 index/hash 可信时复用媒体。原中断目录不会被删除或改写。

## 媒体范围

媒体补全以 base 保存的实体记录为清单，按以下规则处理：

- ShotGrid 托管的 upload、Attachment 原文件、image 与 filmstrip 全部下载。
- 勾选可选 Copy 后，`PublishedFile.path` 以及 `link_type=local` 的 PublishedFile 路径才从当前机器已经挂载且可读的本地盘或 NAS 复制。工具不会自动 mount 存储，也不会保存 NAS、文件服务器或云存储凭据。
- 路径中显式出现 `%04d`、`%d`、`####`、`@@@@`、`$F4` 或 `$F` 时按序列展开并复制匹配帧；没有这些显式 token 的路径不会被擅自当作序列。
- PublishedFile 上明确属于媒体的 HTTPS 地址使用隔离下载流程处理。
- 普通业务 web URL 只保留在实体元数据中，不视为媒体，不抓取其页面或附件。

若 supplement 在 base 生成后才重新向 ShotGrid 获取 image 下载入口，取得的可能是执行补全时的当前文件，而不是 base 时间点的历史原件。此类来源会在 manifest 与日志中标记为 `current_refetch`，不能当作历史时点 `exact`；直接由 base 内已固定内容证明的项目才可使用相应的 exact 语义。

## 自适应带宽与并发

媒体阶段把网络 download 与本地/NAS copy 分成独立资源池。两者都从低并发开始，根据实际吞吐、失败率和重试情况动态升降，不要求操作者猜 worker 数。UI 会持续显示已处理数量、字节进度与 ETA，并分别显示 ShotGrid 下载的当前带宽/并发和本地/NAS 复制的当前带宽/并发，不合并成一个速率；界面不提供带宽或并发选项。download 与 copy 的 ETA 分别使用各自最近 100 个已完成任务耗时的滑动平均，再按各自当前并发计算；样本少于 10 个时显示“校准中”，不使用全程累计平均。

动态调节只改变调度速度，不放宽 required media、hash 或完成门禁。网络失败会进行有上限的自动重试；程序重开后会重新校验断点，hash 正确的已完成文件直接跳过，只补失败、损坏或缺失项。1.0.2 起媒体任务会固定启动时的输出目录设备与 inode 身份；若 NAS/外置盘断挂、目录消失或被另一个挂载替换，download 与 copy 队列会立即整体停止，不再把后续几万项逐个记成传输失败，也不会在原挂载点下创建同名本地目录。重新挂载原存储并再次运行即可继续同一个 `.incomplete`。

失败项会在 `media/index.json` 与 `logs/errors.json` 中保存经过脱敏、限长的 `code`、异常类型和说明，便于区分网络超时、ShotGrid locator、transient media 与输出存储断开；URL 签名、API Key 和本机绝对路径不会写入错误详情。网络受限、存储断连或文件缺失时，结果仍是 `.incomplete`，不会以降级完成代替失败。retired Attachment 会先使用 ShotGrid 返回的安全 HTTPS upload locator；旧 locator 失效时仍按 retired 记录重新查询后重试。

macOS Finder 可能在备份目录内自动创建或更新 `.DS_Store`。它不属于 ShotGrid 数据：1.0.1 起不会写入新快照的校验清单，校验旧快照时也会忽略旧清单中的 `.DS_Store`，因此无需为了这类平台元数据重新导出实体；其他文件仍按原有 SHA-256 严格校验。

## 输出结构与可移植性

```text
<output>/
  latest.txt                         # 指向当前已完成 base
  <base_snapshot_id>/
    COMPLETED.json
    manifest.json
    checksums.sha256
    schema/
    entities/
    links/
    logs/
  media_supplements/
    <base_snapshot_id>/
      <supplement_id>/
        COMPLETED.json
        manifest.json
        checksums.sha256
        media/
          <EntityType>/
            <id_shard>/
              <record_id>/
                <field>/
                  <file>
                  <frame_shard>/<sequence frames>
        logs/
      <supplement_id>.incomplete/
```

媒体按 entity → ID 分片 → record ID → field 分层；序列帧数量较大时再增加 frame 分片。任何全站 `media/` 或 `attachments/` 目录都不得把几万份文件平铺在同一级。具体分片名和文件相对路径以 supplement manifest 为准。supplement 的 lineage 至少锁定 base snapshot ID 及 base 关键清单的 SHA-256；自身 `checksums.sha256` 覆盖补全 payload，`COMPLETED.json` 再锁定 supplement manifest 与 checksum 清单。这样可以证明“哪一个补全包属于哪一个 base”，而无需改动 base。

跨电脑保存或迁移时，必须一起复制 `<base_snapshot_id>/` 与 `media_supplements/<base_snapshot_id>/`；只复制其中一边不能称为完整媒体备份。`latest.txt` 是自动发现 base 的便捷指针，不能替代 base 本体、lineage、hash、checksum 或完成回执。

目录在 macOS / Linux 上使用私有权限。base 与 supplement 都可能包含人员、项目、路径、签名媒体 URL 和实际媒体内容，应放在 FileVault 或其他加密磁盘中，并制定独立保留策略；不要提交 Git。

## 只读校验

校验实体 base 不会连接或写入 ShotGrid：

```bash
.venv/bin/python tools/shotgrid_backup/snapshot_verify.py \
  /path/to/base_snapshot --verify --require-full
```

返回 `"ok": true` 只证明该 base 在文件层面通过当前实体快照合同。媒体是否完整还必须以同一 base lineage 下 supplement 自己的 manifest、`checksums.sha256`、`COMPLETED.json` 和非 `.incomplete` 状态为准。

同时校验 base 与媒体补全包：

```bash
.venv/bin/python tools/shotgrid_backup/snapshot_verify.py \
  /path/to/base_snapshot --verify --require-full \
  --media-supplement /path/to/media_supplement --require-all-media
```

只有这个命令返回 `"ok": true`，并且 `media_payload_scope` 符合本次是否启用外部 Copy 的策略，才表示对应的实体与 required media 组合通过离线校验。

## CLI（高级用途）

图形应用负责“复用已有 base，或先生成新 base，然后自动补媒体”的完整流程。CLI 为自动化和诊断保留显式实体选项：

```bash
export SHOTGRID_URL="https://your-site.shotgrid.autodesk.com"
export SHOTGRID_SCRIPT_NAME="local_backup"
export SHOTGRID_SCRIPT_KEY="..."
export SHOTGRID_HTTP_PROXY="127.0.0.1:7892"  # 可选

.venv/bin/python tools/shotgrid_backup/backup.py --check
.venv/bin/python tools/shotgrid_backup/backup.py --all-readable
```

CLI 不带 `--all-readable` 时保留历史上的核心实体默认集；不要把这种快照称为完整站点实体备份。`--entities` 与 `--updated-since` 只用于高级诊断或增量实验，不能替代周期性完整 base。仅生成 base 也不能宣称全媒体完成。

只探察 schema、实体计数和媒体字段覆盖而不导出业务记录值：

```bash
.venv/bin/python tools/shotgrid_backup/audit.py --http-proxy 127.0.0.1:7892
```

报告默认写入 Git 忽略的 `.local/audits/latest.json`；它仍包含站点结构与数量信息，应按内部诊断数据处理。

## 已知边界

- ShotGrid API 不提供跨实体数据库事务。全量模式使用稳定 ID keyset 扫描，增量模式用时间上界；运行中新增、更新或删除仍可能形成极小的一致性窗口，应在业务低峰执行并保留多代 base。
- API 已硬删除且不可见的数据无法事后备份。
- PublishedFile 外部路径只有在执行 supplement 的机器上已经挂载并可读时才能复制；工具不自动挂载外部存储，也不管理其凭据。
- 普通业务 web URL 永远只存元数据，不纳入媒体抓取。PublishedFile 的 HTTPS 媒体仅走隔离下载流程，不能据此推断任意 URL 都会被下载。
- `current_refetch` 只能证明补全执行时取得的当前媒体，不能证明它与 base 时间点的历史文件完全相同。
- schema 证明源站当时有哪些字段，但未来逻辑恢复仍需在目标站处理 custom field、系统实体、来源 ID 到目标 ID 映射与链接循环；当前项目没有恢复实现。

完整合同见 [docs/snapshot_restore_contract.md](docs/snapshot_restore_contract.md)，安装、已有 base 补全与故障排查见 [docs/user_guide.md](docs/user_guide.md)。

## 作者

Wangbo
