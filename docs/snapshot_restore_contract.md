# ShotGrid 实体快照、媒体补全与未来恢复合同

## 1. 目的与非目标

本合同定义两类不可变产物：

1. 实体快照（base）保存 ShotGrid schema、记录、关系与来源身份。
2. 媒体补全包（media supplement）以某个已完成 base 为事实源，保存该 base 对应的 required media 及可验证 lineage。

图形应用在所选输出目录存在有效 `latest.txt` 时，自动发现其指向的 base，不重新拉取实体，不修改原快照，只创建 `media_supplements/<base_snapshot_id>/<supplement_id>/`。没有可复用 base 的新备份先发布 base，再自动运行媒体补全。

当前 macOS App 只读源 ShotGrid，并从当前机器已经挂载、可访问的本地盘或 NAS 复制 PublishedFile。它不向 ShotGrid 或外部存储写入，也不承担恢复。下文恢复顺序是未来独立逻辑恢复或迁移工具的设计合同，不表示恢复功能已经实现。

未来恢复是“逻辑恢复”，不是数据库镜像：ShotGrid 不允许指定原始 record ID，目标站会生成新 ID，因此所有单实体、多实体和 connection 引用必须经过 `(source_type, source_id) -> (target_type, target_id)` 映射。

## 2. Base 完成门禁与不可变性

正式 base 必须同时具有：

- `manifest.json` 且 `format=shotgrid_portable_snapshot`、`schema_version=3`、`status=complete`。
- `checksums.sha256` 覆盖除 manifest、checksum 自身和完成回执之外的全部 payload。
- `COMPLETED.json` 包含相同 snapshot ID，并锁定 manifest 与 checksum 清单 SHA-256。
- `logs/errors.json` 为空数组。
- 目录名不以 `.incomplete` 结尾。

任何 hard gate 失败均不得更新输出根的 `latest.txt`。发布后的 base 不得因媒体补全而加入、删除或改写任何文件；base 完成只证明实体快照合同通过，不自动证明媒体完整。

## 3. 实体范围合同

UI 使用 `scope=all_readable_entities`：读取 authenticated `schema_entity_read()` 返回的全部实体，而不是按 UI visibility 或固定业务清单过滤。每类实体保存全部 `schema_field_read()` 可读字段，包括 UI hidden 但 API 可读的实体与字段；因此整个 base 必须按敏感数据处理。

每类实体均显式包含 archived project 记录。只有经探测确认 `retired_only=True` 返回独立记录集时才执行 retired 查询，避免 EventLogEntry 等不支持 retired 的实体被重复导出。

固定业务清单不能代表关系闭包：HumanUser、Status、Step、TaskTemplate、PublishedFile、Reply、PlaylistVersionConnection、TaskDependency、Cut/CutItem 及各种 connection 都可能承载未来恢复所需信息。因此完整模式必须以当次 authenticated schema 为事实源，不得回退到默认实体清单。

## 4. 记录格式

`entities/<EntityType>.jsonl` 每行：

```json
{
  "source": {"type": "Shot", "id": 123},
  "state": "active",
  "record": {"type": "Shot", "id": 123, "code": "sh010"}
}
```

来源身份与 payload 分离，避免把备份控制字段注入 ShotGrid 原记录。记录按 state 分组、组内 source ID 严格递增。manifest 对每个实体保存相对文件路径、active/retired 计数、字段数、retirement 支持和文件 SHA-256。

`links/<EntityType>.jsonl` 把每个 entity / multi_entity 字段展开为 `source、state、field、ordinal、target(type/id/name)`。ordinal 从 0 开始，保留多链接顺序；未来恢复只使用 type+ID mapping，name 仅用于人工诊断。PlaylistVersionConnection、TaskDependency 与其它 connection 实体仍作为普通实体完整保存，其自身排序和属性字段不会被关系索引替代。

## 5. Schema 合同

`schema/entities.json` 保存源站实体 schema；`schema/fields/<EntityType>.json` 保存当时账号可读的完整字段元数据。未来恢复工具必须以目标 schema 重新分类：

- builtin compatible：目标内置字段兼容。
- custom compatible：同 code、data type 和 link target 兼容。
- custom create：目标缺失且管理员明确允许创建。
- readonly preserve only：源值只保留，不能回写。
- conflict / unsupported：阻断或人工处理。

不能只凭 `sg_` 前缀或显示名判断字段身份。同 code 不同 data type 是 hard conflict。

## 6. Supplement 发现、lineage 与完成门禁

应用只把 `<output>/latest.txt` 指向且通过 base 完成门禁的目录作为自动发现对象。已发现的 base 直接进入媒体发现，不重新执行实体全量导出。

每个 supplement manifest 必须记录自己的 supplement ID，并以不可含糊的 lineage 绑定：

- base snapshot ID。
- base `manifest.json` 的 SHA-256。
- base `checksums.sha256` 的 SHA-256。
- 用于媒体发现的 base 格式与来源站身份。

supplement 自身必须满足：

- 所有 required media 均有成功的 download/copy 结果、大小和 SHA-256。
- `checksums.sha256` 覆盖补全包内全部正式 payload。
- `COMPLETED.json` 包含相同 supplement ID 和 base lineage，并锁定 supplement manifest 与 checksum 清单的 SHA-256。
- manifest 没有 unresolved required media，目录名不以 `.incomplete` 结尾。

任一 required media 无法访问、下载、复制或校验时，本轮目录必须保留为 `<supplement_id>.incomplete`，不得产生可被误认的有效完成回执，也不得宣称“全媒体完成”。重启时可以原子更新 lineage/策略匹配的 `.incomplete` 断点；不匹配时创建新的 supplement ID。不得修改既有完整 supplement 或 base。

### 6.1 旧版 `.incomplete` 的受约束 salvage

旧版单目录任务可能在实体导出完成后进入媒体阶段，并在中断时留下 `.incomplete` 及已经下载的媒体。新版只能在自动验证 `entity_complete` 证据、实体/schema/关系计数、关键 hash 与实体阶段错误状态后执行 salvage：

- 从已证明完整且未被改写的实体 payload 重建新的 sealed base 数据基线。
- 已有媒体只有在来源、大小与 hash 可验证时才可计为完成；同一文件系统可使用不共享可变 inode 的 copy-on-write clone，否则采用独立的校验复制。
- 新 supplement 必须重新建立自己的 lineage、manifest、checksum 与完成回执，并只获取缺失的 required media。
- 原 `.incomplete` 不得改名、覆盖或删除；已复用到新 artifact 的 payload 必须保持独立不可变语义。
- 任一 `entity_complete` 证据无法证明、计数不一致、hash 不符或存在实体阶段错误时，必须拒绝 salvage，不能按“已有文件很多”推定实体完整。

## 7. Required media 范围

媒体发现只依据 base 中保存的实体、字段、record ID 与值，并按以下来源分类：

### 7.1 ShotGrid 托管媒体

- 所有 ShotGrid 托管 upload 均为 required media。
- `Attachment.this_file` 原文件全部下载。
- image 与 filmstrip 全部下载。
- 下载结果必须保存来源 entity/record/field、相对路径、字节数和 SHA-256。

不能把临时签名 URL 本身当作媒体资产；只有落地 bytes 与可校验 hash 才算已下载。

### 7.2 PublishedFile 本地或 NAS 路径

- `PublishedFile.path` 以及 PublishedFile 值中 `link_type=local` 的路径均进入 copy 发现。
- 只从执行机器当前已经挂载且可读的本地盘或 NAS 复制。工具不自动 mount，不代管映射，不提示或保存外部存储凭据。
- 远程机器没有相同挂载、路径不存在或权限不足时，相关 required media 未满足，supplement 保持 `.incomplete`。
- 路径明确包含 `%04d`、`%d`、`####`、`@@@@`、`$F4` 或 `$F` 时必须展开所有匹配帧；没有显式 token 时按单文件处理，不凭相似文件名猜测序列。

### 7.3 PublishedFile HTTPS 媒体与普通 URL

- PublishedFile 语境中明确属于媒体的 HTTPS 地址进入隔离下载流程，和普通浏览或页面抓取分开执行。
- 普通业务 web URL 不属于 required media，只保留在实体 JSONL 中作为元数据；不得抓取页面、递归链接或网页附件。
- 不能因为某个字段的数据类型是 URL 就默认下载，也不能把普通 URL 失败计成媒体缺失。

这一区分取代“PublishedFile 永远 metadata-only”的旧口径：可访问的本地/NAS PublishedFile 和合格的 PublishedFile HTTPS 媒体可以进入 supplement，普通业务 URL 仍然只存元数据。

## 8. 媒体时点与 `current_refetch`

事后补全 image 时，base 内临时签名 URL 可能已经过期。若工具根据 base 的 entity、record ID 与 field 重新向 ShotGrid 请求下载入口，得到的可能是执行 supplement 时的当前 image，而不是 base 生成时的历史原件。

manifest 与日志必须区分至少两种时点语义：

- `exact`：有可校验依据证明 bytes 对应 base 固定的历史内容。
- `current_refetch`：事后向 ShotGrid 获取了当前媒体，不能证明与 base 时点一致。

含 `current_refetch` 的完整 supplement 可以说明 required media 获取流程已完成，但不得宣称所有媒体都是历史时点 exact。时点语义不得被完成回执或 checksum 掩盖。

## 9. 目录与分片合同

supplement 位于：

```text
<output>/media_supplements/<base_snapshot_id>/<supplement_id>/
```

媒体 payload 必须按 entity → ID 分片 → record ID → field 分层；序列帧数量大时再增加 frame 分片：

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

ID 分片和 frame 分片的实际规则必须稳定并写入 manifest。Attachment 使用 `Attachment` entity、attachment ID 与 `this_file` field 进入同一层次，不创建全站平铺的 attachment 文件池。任何全站 `media/` 或 `attachments/` 目录都不得把几万份文件平铺在同一级。

所有索引只记录相对于 supplement 根的 POSIX 路径，不依赖当前机器的输出绝对路径。源 PublishedFile 的原始路径可作为受保护元数据保留，但不能作为补全包内部定位方式。

## 10. 自适应 download/copy 调度

网络 download 与本地/NAS copy 必须使用独立资源池，从低并发开始，并根据观测到的吞吐、错误率与重试情况动态升降。UI 持续显示项目/字节进度和 ETA，并分别显示 ShotGrid 下载的当前带宽/并发和本地/NAS 复制的当前带宽/并发，不得合并成单一速率。download 与 copy 的 ETA 必须分别使用各自最近 100 个已完成任务耗时的滑动平均，再按各自当前并发计算；样本少于 10 个时显示“校准中”，不得改用全程累计平均。媒体并发与带宽不作为用户选项，避免固定参数在不同网络、磁盘和 NAS 条件下造成错误判断。

自适应策略只能改变调度，不能改变 required 集合、错误语义或完成门禁。降并发后仍不可访问的媒体必须留下 unresolved 结果并使 supplement 保持 `.incomplete`。

## 11. 一致性模型

base 全量采集使用 `id > last_id` keyset 分页，不用 `updated_at` 过滤，避免漏掉极少数 `updated_at=null` 记录。增量模式才使用 `updated_since < updated_at < snapshot_upper_bound`。keyset 避免 offset 分页在并发写入时跳页，但 ShotGrid API 仍不提供跨实体事务。

base manifest 必须明确记录 `keyset_full_best_effort` 或 `bounded_incremental_best_effort`。建议在低峰运行、保留多代完整 base，并在重大灾难恢复前结合 EventLogEntry 与业务样本复核。硬删除且 API 已不可见的数据无法由任何事后备份找回。

supplement 的实体视图固定于其 lineage 对应的 base，但 `current_refetch` 反映补全执行时的当前媒体；两种时间边界必须同时保留，不能把它们描述成一个数据库事务时间点。

## 12. 日志合同

base `logs/events.jsonl` 每行至少包含 `seq`、`at`、`event`；具体事件补充 entity、batch、records、source ID、field、size 或 error。seq 在进程锁内递增；事件写入后 flush + fsync。分页进度在该页记录写入完成后才发出。

supplement 日志还必须能审计媒体发现、来源分类、download/copy 池调节、实时进度、重试、序列展开、`exact` / `current_refetch`、成功结果和 unresolved required media。日志不等同于断点续跑 checkpoint，也不能替代 manifest 和 checksum。

错误使用 `{type, message}` 结构，message 会去除已知 key/token/Authorization/proxy userinfo 并限制长度。日志禁止记录 API Key、Authorization header、请求正文、业务记录正文、外部存储凭据或完整临时签名 URL。

## 13. 跨电脑可移植性

跨电脑复制必须保留以下成对结构：

```text
<output>/<base_snapshot_id>/
<output>/media_supplements/<base_snapshot_id>/
```

`latest.txt` 应随输出根复制以便自动发现，但它只是便捷指针。校验必须以 base 和 supplement 各自的 `manifest.json + checksums.sha256 + COMPLETED.json` 为准，再确认 supplement lineage 指向实际随附的 base。只复制 base 或只复制 supplement 均不能称为完整媒体备份。

SHA-256 能发现传输损坏和普通篡改，但不提供持密钥的真实性证明。若未来需要对抗恶意篡改，应在完成回执外增加由系统 keyring 管理的 Ed25519 签名；签名密钥不能与备份放在一起。

## 14. 未来恢复顺序

未来独立恢复工具应固定执行：

1. 离线校验 base 与选定 supplement 的完成回执、全部 payload SHA-256 和 lineage。
2. 明确列出 `current_refetch`，由操作者决定其是否满足恢复目标。
3. 只读连接目标站并生成 schema diff。
4. 由操作者提供 Project、Status、Step、HumanUser、LocalStorage、PublishedFileType 等基础记录映射。
5. 创建主体记录并持久化 source-to-target ID map。
6. 第二阶段回填 entity/multi_entity 字段与 connection 实体，处理循环引用。
7. 按顺序恢复 TaskDependency、Version、PublishedFile、Note/Reply、PlaylistVersionConnection 等依赖。
8. 按 manifest 将 upload、Attachment、image、filmstrip 和 PublishedFile 媒体重新关联到目标记录；外部路径必须应用经批准的映射。
9. 所有关系和媒体完成后，最后处理 retired 状态。
10. 重算计数、抽样业务关系、保存独立 restore log 与 reconciliation report。

目标站已有数据时禁止仅按显示名自动合并。恢复映射必须 append-only、可 checkpoint、以 source type+ID 为唯一源键，确保重跑幂等。

## 15. 恢复前人工输入

未来恢复至少需要：目标站地址、最小写权限 Script、目标 schema 处理决策、基础实体 mapping、冲突策略、PublishedFile/LocalStorage 路径映射、对 `current_refetch` 的接受策略、维护窗口和验收人。缺少任一必需 mapping 或存在 schema hard conflict 时，未来 dry-run 必须标记 not ready 并禁止 apply。

这些要求是未来工具的安全边界。当前仓库没有可执行的 restore、dry-run apply 或恢复按钮。

## 作者

Wangbo
