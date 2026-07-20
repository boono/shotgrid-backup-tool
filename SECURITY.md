# 安全说明

## ShotGrid 凭据

不要在 issue、日志、截图、manifest 或配置文件中提交 ShotGrid API Key。若 Key 曾发送到聊天、工单或公开位置，请立即在 ShotGrid 中轮换。

图形应用会在预检查后清空输入框，并使用短时、单次内存 handle 启动任务；本工具不会主动把 API Key 写入 base 或 media supplement。浏览器、操作系统和第三方诊断工具仍可能有自己的内存或崩溃记录策略，因此推荐使用专用、只读、最小权限的 Script。

## 外部存储与 PublishedFile

PublishedFile 本地路径和 `link_type=local` 只会从当前机器已经挂载且可读的本地盘或 NAS 复制。工具不会自动 mount 存储，不会要求或保存 NAS、SMB、NFS、文件服务器或云存储凭据，也不会尝试绕过操作系统权限。需要外部存储时，应由操作者在系统层完成可信挂载，并只授予任务所需的读取权限。

跨电脑后路径可能指向不同设备。不要为了让 supplement 通过而把高权限凭据写入配置、启动脚本或仓库；无法安全访问的 required media 应保持 `.incomplete`。

## 网络媒体边界

ShotGrid 托管的 upload、Attachment、image 和 filmstrip，以及 PublishedFile 上明确属于媒体的 HTTPS 地址，才进入媒体下载。PublishedFile HTTPS 媒体使用隔离下载流程，不能扩展为通用网页抓取器。

普通业务 web URL 只存元数据，不请求页面、不递归链接、不下载网页附件。这个边界既避免误抓敏感站点，也降低由记录内容触发任意网络访问的风险。日志不得记录 Authorization header、API Key、代理 userinfo 或完整临时签名 URL。

## Base、supplement 与中断目录

base、`media_supplements/` 和旧版 `.incomplete` 可能包含人员信息、项目内容、内部路径、临时签名 URL、源文件与完整序列。请使用 FileVault 或其他加密存储，限制目录权限，不要上传到公共 GitHub 仓库。

媒体补全不得修改原 base。跨电脑迁移必须一起保护 base 与其 `media_supplements/<base_snapshot_id>/`；lineage、SHA-256、`checksums.sha256` 和 `COMPLETED.json` 用于发现损坏与普通篡改，但不等同于加密或持密钥签名。

旧版 `.incomplete` salvage 只能在 `entity_complete` 与相关 hash/计数可验证时进行。已完成文件只允许通过 APFS clone 或独立校验复制进入新 artifact，不允许跨 artifact 共享可变 hardlink inode。无法证明来源或完整范围的目录应保留供审计或隔离处理，不能手工去掉 `.incomplete` 后缀。

## 报告漏洞

公开报告前请移除站点地址、record 内容、API Key、代理凭据、外部路径、内部主机名、签名 URL 和真实媒体。不要在漏洞复现中附带真实 base、supplement 或旧版中断目录。

## 作者

Wangbo
