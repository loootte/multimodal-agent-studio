# 本地存储加密与会话隔离（12.F）

对应 [#62](https://github.com/loootte/multimodal-agent-studio/issues/62)，父 issue [#56](https://github.com/loootte/multimodal-agent-studio/issues/56)，决定记在 [ADR 0009](adr/0009-handle-scope-and-egress-audit.md) 的「12.F 实现定稿」。

## 解决什么

12.A–12.E 管的是「本地内容不出本机」。12.F 管「出了本机的磁盘」：把 `data/` 拷到别的机器（备份盘、网盘同步、被偷的笔记本）时，没有主密钥就读不出任何会话正文和文件字节；会话之间用不同的派生密钥，互相挪文件也解不开。

网关、运行时、工具、模型都不变：它们只拿解密后的本机内存对象，模型仍然只看到句柄。

## 三种模式

`MAS_STORAGE_ENCRYPTION`：

| 取值 | 模式 | 行为 |
| --- | --- | --- |
| 不设 / `off` | 明文（默认） | 和以前一样：`sessions/<id>.json` + `artifacts/blobs/`（按 sha256 去重） |
| `on` | 加密 | 会话文件是 `mas.enc/1` 信封；内容 / 附件字节进 `artifacts/sealed/<会话>/`；空数据目录首次启动自动生成主密钥 |
| `memory` | 仅内存会话（演示） | 会话和字节只在进程内存里，`sessions/`、`artifacts/` 一个文件都不写，停服务即没 |

数据目录一旦加密（有 `<data_dir>/storage.json`，`mas.storage/1`），就永远按加密打开：把环境变量改回 `off` 也不会退回明文，只会因为缺密钥报错。

**默认仍是明文**，原因：现有用户的数据要显式迁移；丢主密钥 = 数据全丢，必须由用户自己决定并做好备份。

## 怎么加密的

- 算法：AES-256-GCM（`cryptography` 库；只在开启加密时需要，没装就报 `storage_crypto_unavailable`，绝不悄悄写明文）。
- 每个会话一把密钥：`HKDF-SHA256(主密钥, salt="mas.storage/1", info="mas/session/<session_id>")`，只在内存里。
- 会话文件：`{"schema":"mas.enc/1","alg","key_id","scope":"session","session_id","nonce","ct"}`，头部整体作 AAD——挪到别的会话名下、换主密钥、改一个字节都解不开。
- 字节：`MASE\x01 | nonce | 密文`，AAD 绑 `key_id | session_id | sha256`，解开后再核一遍 sha256；文件名是会话密钥上的 HMAC，不暴露明文的 sha256（别人不能「猜一张图 → 比哈希」）。加密时不跨会话去重。
- `key_id`：主密钥的 HMAC 指纹前 16 位，用来核对数据目录和密钥是否配套，不可逆。

## 主密钥放哪

查找顺序：

1. 环境变量 `MAS_MASTER_KEY`（base64url 的 32 字节），适合放进密码管理器 / 系统凭据后注入；
2. `MAS_MASTER_KEY_FILE` 指向的文件；
3. 默认 `~/.mas/master.key`（Windows 是 `C:\Users\<你>\.mas\master.key`），权限 0600，已存在不覆盖。

主密钥**不许**放进数据目录或仓库（`storage_key_location`）；`.gitignore` 挡了 `*.key`、`master.key`、`*.keybackup*`、`.mas/`。12.D 沙箱把密钥目录和数据目录一起列为禁读，第三方工具包读不到；子进程也不继承 `MAS_MASTER_KEY`（环境变量白名单）。

## 命令行（先停聊天服务）

```
python apps/web/storage_tool.py status                    # 模式、key_id、明文残留，不打印密钥
python apps/web/storage_tool.py encrypt                   # 明文 → 加密；没有主密钥就生成
python apps/web/storage_tool.py verify                    # 逐个解开会话和字节，坏了几个
python apps/web/storage_tool.py export-key --out D:\backup\mas.keybackup.json
python apps/web/storage_tool.py import-key --in D:\backup\mas.keybackup.json
python apps/web/storage_tool.py decrypt --yes             # 加密 → 明文（不想用了）
```

`--data-dir` 默认是 `CHAT_DATA_DIR` 或 `apps/web/data`。迁移后用 `MAS_STORAGE_ENCRYPTION=on` 启动，或者不设也行（标记在，照样加密打开）。

网页「设置 → 本地存储」只显示模式、算法、key_id 和明文残留；导入 / 导出密钥只走命令行，密钥不经过 HTTP。`GET /api/storage` 只给本机。

## 备份与恢复手册

**丢了主密钥和备份，加密的会话就再也打不开。没有后门。**

开启后马上做：

1. `export-key --out <U 盘或密码管理器附件>`：口令（≥12 个字符）经 scrypt（n=2^15, r=8, p=1）派生，再用 AES-GCM 包住主密钥；备份文件 `mas.keybackup/1` 里没有明文密钥。口令从 `MAS_BACKUP_PASSPHRASE` 读，没有就交互输入两次。备份不许写进数据目录或仓库。
2. 把口令记在别处（密码管理器）。备份文件和口令分开放。
3. 备份 `data/` 本身照常（拷目录即可，拷出去是密文）。

换机器 / 重装后恢复：

1. 把 `data/` 拷回来；
2. `import-key --in <备份文件>`，输入口令，写回 `~/.mas/master.key`（或 `--key-file` 指定位置）；输出的 `matches_data_dir` 应为 `true`；
3. `verify` 全部通过后再启动服务。

出错码：

| 错误码 | 意思 | 怎么办 |
| --- | --- | --- |
| `storage_key_missing` | 数据目录加密了，但找不到主密钥 | 用 `import-key` 恢复，或设 `MAS_MASTER_KEY` |
| `storage_key_mismatch` | 主密钥和数据目录的 key_id 对不上 | 换回正确的密钥；不要新建密钥覆盖 |
| `storage_migration_required` | 设了 `on`，但目录里还有明文会话 | 停服务跑 `encrypt` |
| `storage_key_location` | 密钥 / 备份要写进数据目录或仓库 | 换个位置 |
| `storage_key_exists` | 目标位置已经有主密钥 | 不会覆盖；先确认那把密钥是不是你要的 |
| `storage_backup_invalid` | 备份文件坏了或口令不对 | 换备份 / 口令 |
| `storage_passphrase_weak` | 口令短于 12 个字符 | 换长口令 |
| `storage_crypto_unavailable` | 没装 `cryptography` | `pip install cryptography` |
| `storage_decrypt_failed` | 某个会话文件 / 字节解不开（被挪、被改、密钥不对） | 该会话在列表里消失；遥测记 `security.storage_decrypt_failed`（只有会话 id 和原因码） |

## 仅内存会话（演示模式）

`MAS_STORAGE_ENCRYPTION=memory` 启动：适合售前演示或处理一次性敏感材料。会话、本地内容、附件、生成过程都只在内存；页面照常用；停服务就没了。仍会写的文件：遥测 / 出站审计（本来就无正文）、供应商设置；轨迹导出不留正文摘要。

## 遥测事件（无密钥、无正文、无路径）

- `security.storage_key_created`：`key_id`、`key_source`
- `security.storage_decrypt_failed`：`session_id`、`reason_code`（`wrong_key` / `wrong_scope` / `tampered` / `not_envelope`）
- `security.storage_migrated`：`direction`（`encrypt` / `decrypt`）、`key_id`、会话 / 字节 / 旧文件个数

## 不覆盖的（已知局限）

- ComfyUI 生成的图片（`poc/comfyui/output`、生成图工件库）不加密。
- 遥测、出站审计本来就无正文，不加密；加密 / 内存模式下轨迹导出只留结构和字数，不留正文摘要。
- 主密钥默认是文件或环境变量；桌面宿主（13.B）默认放系统钥匙串（Windows 凭据管理器 / macOS 钥匙串 / Secret Service），Web 模式要显式 `MAS_KEYCHAIN=on`。`storage_tool.py --keychain encrypt` 会把新主密钥写进钥匙串。
- 迁移后删除明文在 SSD / 写时复制文件系统上不等于擦除；高敏感场景请在新磁盘上开加密后再导入。
- 进程内存、交换文件、休眠文件不在范围。
- Web 模式默认关闭（见上）；桌面宿主默认打开，已有明文数据时启动前拒绝并给迁移命令（见 docs/desktop.md 13.B）。

## 怎么检查

- `python apps/web/check.py` 的 `_storage_crypto_cases`：盘上读不到正文和 sha256、同一密钥重开、`/api/storage` 不带密钥 / 路径、会话文件和字节挪位解不开、篡改、拷走没密钥 / 换密钥 / 环境变量降级都打不开、密钥位置、备份往返和口令错、迁移与 verify / decrypt、仅内存不落盘、轨迹不留正文、性能、缺库报错、沙箱禁读密钥目录、`.gitignore`。
- `python evals/run.py`：`security.storage_encrypted_at_rest`、`security.storage_session_isolation`、`security.storage_key_required`、`security.storage_memory_only`（加密 / 仅内存站点跑一轮完整对话，供应商仍只见句柄）。
