# ShotGrid 快照与未来恢复合同

## 1. 目的与非目标

本合同定义备份产物必须保存什么，才能在未来编写独立恢复/迁移工具。当前 macOS App 只读源 ShotGrid 并生成快照，不承担恢复写入。

未来恢复是“逻辑恢复”，不是数据库镜像：ShotGrid 不允许指定原始 record ID，目标站会生成新 ID，因此所有单实体、多实体和 connection 引用必须经过 `(source_type, source_id) -> (target_type, target_id)` 映射。

## 2. 完成门禁

正式快照必须同时具有：

- `manifest.json` 且 `format=shotgrid_portable_snapshot`、`schema_version=3`、`status=complete`。
- `checksums.sha256` 覆盖除 manifest、checksum 自身和完成回执之外的全部 payload。
- `COMPLETED.json` 包含相同 snapshot ID，并锁定 manifest 与 checksum 清单 SHA-256。
- `logs/errors.json` 为空数组。
- 目录名不以 `.incomplete` 结尾。

任何 hard gate 失败均不得更新输出根的 `latest.txt`。

## 3. 范围合同

UI 使用 `scope=all_readable_entities`：读取 authenticated `schema_entity_read()` 返回的全部实体，而不是按 UI visibility 或固定业务清单过滤。每类实体保存全部 `schema_field_read()` 可读字段，包括 UI hidden 但 API 可读的实体与字段；因此整个快照必须按敏感数据处理。

每类实体均显式包含 archived project 记录。只有经探测确认 `retired_only=True` 返回独立记录集时才执行 retired 查询，避免 EventLogEntry 等不支持 retired 的实体被重复导出。

固定业务清单不能代表关系闭包：HumanUser、Status、Step、TaskTemplate、PublishedFile、Reply、PlaylistVersionConnection、TaskDependency、Cut/CutItem 及各种 connection 都可能承载恢复所需信息。因此完整模式必须以当次 authenticated schema 为事实源，不得回退到默认实体清单。

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

## 5. schema 合同

`schema/entities.json` 保存源站实体 schema；`schema/fields/<EntityType>.json` 保存当时账号可读的完整字段元数据。未来恢复工具必须以目标 schema 重新分类：

- builtin compatible：目标内置字段兼容。
- custom compatible：同 code、data type 和 link target 兼容。
- custom create：目标缺失且管理员明确允许创建。
- readonly preserve only：源值只保留，不能回写。
- conflict / unsupported：阻断或人工处理。

不能只凭 `sg_` 前缀或显示名判断字段身份。同 code 不同 data type 是 hard conflict。

## 6. 媒体合同

- `Attachment.this_file.link_type=upload`：下载原始 bytes 到 `attachments/`，index 保存 attachment ID、retired 状态、相对文件名、大小和 SHA-256。
- image、filmstrip_image 与 URL 字段中 `link_type=upload`：下载到 `media/<EntityType>/`，index 保存 source type/id、field、state、相对路径、大小和 SHA-256。
- 普通 URL、本地路径或无可下载 URL 的媒体：记录值仍保存在实体 JSONL，manifest 计入 `media.metadata_only`。
- PublishedFile/LocalStorage：路径元数据属于本快照，NAS、本地盘、对象存储文件本体不属于 ShotGrid API 备份，必须由外部存储方案另行保护。

临时签名 URL 不是恢复资产；只有本地 bytes + size + SHA-256 才算已下载媒体。

## 7. 一致性模型

全量采集使用 `id > last_id` keyset 分页，不用 `updated_at` 过滤，避免漏掉极少数 `updated_at=null` 记录。增量模式才使用 `updated_since < updated_at < snapshot_upper_bound`。keyset 避免 offset 分页在并发写入时跳页，但 ShotGrid API 仍不提供跨实体事务。

manifest 必须明确记录 `keyset_full_best_effort` 或 `bounded_incremental_best_effort`。建议在低峰运行、保留多代完整快照，并在重大灾难恢复前结合 EventLogEntry 与业务样本复核。硬删除且 API 已不可见的数据无法由任何事后备份找回。

## 8. 日志合同

`logs/events.jsonl` 每行至少包含 `seq`、`at`、`event`；具体事件补充 entity、batch、records、source ID、field、size 或 error。seq 在进程锁内递增；事件写入后 flush + fsync。分页进度在该页记录写入完成后才发出。

错误使用 `{type, message}` 结构，message 会去除已知 key/token/Authorization/proxy userinfo 并限制长度。日志禁止记录 API Key、请求正文、业务记录正文或签名下载 URL。

日志用于审计本次运行，不是断点续跑 checkpoint。`.incomplete` 应重新运行，不应靠事件日志拼成正式快照。

## 9. 未来恢复顺序

独立恢复工具应固定执行：

1. 离线校验完成回执、全部文件、记录计数和媒体 hash。
2. 只读连接目标站并生成 schema diff。
3. 由操作者提供 Project、Status、Step、HumanUser、LocalStorage、PublishedFileType 等基础记录映射。
4. 创建主体记录并持久化 source-to-target ID map。
5. 第二阶段回填 entity/multi_entity 字段与 connection 实体，处理循环引用。
6. 按顺序恢复 TaskDependency、Version、PublishedFile、Note/Reply、PlaylistVersionConnection 等依赖。
7. 上传附件与字段专用媒体并重新挂载到目标 ID。
8. 所有关系和媒体完成后，最后处理 retired 状态。
9. 重算计数、抽样业务关系、保存独立 restore log 与 reconciliation report。

目标站已有数据时禁止仅按显示名自动合并。恢复映射必须 append-only、可 checkpoint、以 source type+ID 为唯一源键，确保重跑幂等。

## 10. 恢复前人工输入

未来恢复至少需要：目标站地址、最小写权限 Script、目标 schema 处理决策、基础实体 mapping、冲突策略、PublishedFile/LocalStorage 路径映射、维护窗口和验收人。缺少任一必需 mapping 或存在 schema hard conflict 时，dry-run 必须标记 not ready 并禁止 apply。

## 11. 安全与可移植性

所有快照内路径必须是 POSIX 相对路径；不得写入某台电脑的用户绝对路径、盘符、临时输出根或凭据。`source.site` 用于识别来源，不用于认证。复制到另一台电脑后以 `COMPLETED.json + manifest.json + checksums.sha256` 重新校验。

SHA-256 能发现传输损坏和普通篡改，但不提供持密钥的真实性证明。若未来需要对抗恶意篡改，应在完成回执外增加由系统 keyring 管理的 Ed25519 签名；签名密钥不能与备份放在一起。
