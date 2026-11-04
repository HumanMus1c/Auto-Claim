# WorkBuddy / Qoder 多账号与手动添加账号可行性

## 结论

技术上可行，但不能把 Trae 的账号模型原样复制过去。Trae 的 `trae_signin.py` 已经把「账号存储、导入去重、手动添加、主号、单账号操作、批量签到、按账号历史」跑通，这套结构可以抽成共享层；真正需要按产品适配的是**凭据来源、请求头、资料接口和账号级缓存**。

| 模块 | 本地多账号导入 | 手动添加账号 | 复杂度 | 主要限制 |
|---|---|---|---|---|
| Qoder CN | 可行：枚举所有 `com.qodercn.app*` 登录目录并逐一解密 | 可行：`QODER_TOKEN` 已证明 token 可独立使用 | 低～中 | token 到期后必须重新导入；设备标识是机器级 |
| WorkBuddy | 可行，但只能读到客户端当前登录的一个账号；切换账号后再次导入 | 可行：需要 `accessToken` + `uid`，可选企业 / 域名 / endpoint | 中～高 | `sym-v1` 只能在导入时借助客户端运行时解密；没有 refresh 实现 |
| Trae（现状） | 已支持，多客户端目录合并 | 已支持 Token / 会话 Cookie | — | 每账号有独立设备号 |

结论是：**Qoder 更适合先做，WorkBuddy 可以复用同一套账号层但需要更多适配**。两者都不建议在没有风控评估的情况下默认开启多账号，因为 WorkBuddy / Qoder 的设备标识是机器级，和 Trae 的「每账号独立设备号」不同。

## 现有实现提供的证据

### Trae 已经具备的共享能力

`trae_signin.py` 中可直接复用的部分：

- `load_store()` / `save_store()`：`accounts[]` + `active_id` + `settings`，Token / Session / 设备号通过 `_seal()` / `_unseal()` 做 DPAPI 加密。
- `import_local()`：扫描多个客户端目录，按 `account_uid` 去重，新增 / 更新账号。
- `add_account()`：解析 Token 或会话 Cookie，按 UID 去重，失败时返回明确错误。
- `remove_account()` / `update_account()` / `set_active()` / `renew_device()`：账号级操作。
- `refresh_account()` / `run_auto()`：逐账号刷新与签到，单个账号失败不影响其他账号，并汇总 `PARTIAL`。
- `monitor()`：把账号、主号、历史、用量、日志汇总给前端。
- 前端 `renderTraeAccounts()`：已经是通用的账号卡片列表，包含启用 / 主号 / 区域 / 成员 / 登录态 / 设备号 / 单账号操作。

这些能力大部分与具体产品无关，适合下沉为共享的账号存储和账号卡片；产品差异放在 Provider 适配层。

### WorkBuddy 的账号边界

`signin.py` 目前按单账号工作：

- `find_auth_file()` 只返回一个凭据文件：`CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info`，可用 `WORKBUDDY_AUTH_FILE` 覆盖。
- 凭据结构包含 `auth.accessToken`、`auth.domain`、`auth.endpoint`、`account.uid`、`account.enterpriseId`，见 `_validate_session()` / `_public_workbuddy_account()` / `build_headers()`。
- 新版 token 可能是 `sym-v1` 加密信封；`resolve_session()` 在每次请求前借助 WorkBuddy 客户端运行时的原生存储接口解密。这个密钥与客户端安装 / 当前 Windows 用户相关，不能假设换一台机器还能解密。
- `build_headers()` 强依赖 `account.uid`，有企业 ID 时还要带 `X-Enterprise-Id` / `X-Tenant-Id`，有域名时带 `X-Domain`。因此手动添加 WorkBuddy 账号**不能只粘贴 token**，至少还需要 `uid`。
- 当前没有使用 `refreshToken`，也没有刷新 access token 的实现；401 只能提示重新登录 / 重新导入。

### Qoder 的账号边界

`qoder_signin.py` 已经有「从外部 token 运行」的能力：

- `_resolve_token()` 优先读取 `QODER_TOKEN`；环境变量模式下 `_auth_dir=None`，仍然可以正常请求，说明手动 token 路线本身成立。
- `find_auth_dirs()` / `load_session()` 会扫描多个 `com.qodercn.app*` 目录，但当前只返回第一个成功解密的会话；其余目录被忽略。
- Windows 会话里有 `token`、`expiresAt`、`user`；macOS 的解密助手目前只返回 `token` / `expiresAt`，没有返回 `user`，做多账号去重时需要补上（或退回按 token 去重）。
- `risk_headers()` 使用 `runtime-info.exe`、`auth.machine-id`、主机名等**机器级**信息，不是账号级设备号；手动 token 也复用这些请求头。
- `QODER_BASE_URL` / `QODER_AUTH_DIR` / `QODER_TOKEN` 已经存在，说明参数化账号来源不需要推翻现有入口。

### 控制台侧的单账号假设

- WorkBuddy 的 `renderWorkbuddyAccount()` 和 Qoder 的 `renderQoderAccount()` 都直接渲染一个 `doctor.account` / `status`，不是账号数组。
- WorkBuddy 的缓存 `_load_store()` 以 `status` / `doctor` / `auto` 为键，没有账号维度；Qoder 的 `qoder_monitor()` 每次查询当前登录态，也没有账号库。
- 计划任务的失败恢复 `_reimport_local()` 对 WorkBuddy / Qoder 是「重读登录文件」，对 Trae 是 `import_local()`；多账号后需要保留单账号语义作为兜底。

## 建议的共享架构

不要直接把 WorkBuddy / Qoder 的凭据塞进 Trae 的 `accounts.json`，建议按产品拆文件、共享实现：

```text
dashboard/account_store.py
  - AccountStore(provider, path)
  - load() / save()
  - add() / update() / remove() / set_active()
  - public_account()（只输出掩码）

signin.py / qoder_signin.py / trae_signin.py
  - 各产品保留自己的 credential / headers / API
  - 增加 scan_local() -> candidates
  - 增加 parse_manual(raw, fields) -> account
  - 增加 probe(account) -> status / profile
```

存储层建议：

```text
%APPDATA%\Auto-Claim\workbuddy\accounts.json
%APPDATA%\Auto-Claim\qoder\accounts.json
%APPDATA%\Auto-Claim\trae\accounts.json
```

每条账号记录至少包含：

```json
{
  "id": "...",
  "label": "...",
  "enabled": true,
  "credential": "DPAPI 封装后的 token / session",
  "profile": {"uid": "...", "name": "...", "region": "CN"},
  "last_status": {},
  "last_checkin_date": null
}
```

Provider 适配层负责：

1. 本地扫描：把客户端当前登录态转成候选账号。
2. 手动解析：把用户粘贴的 token / JSON / Cookie 转成候选账号。
3. 探测：查询状态、资料、到期时间。
4. 领取：单账号签到 / 领取。
5. 请求头：产品特有的 UID / 企业 / 域名 / 设备头。

调度层统一循环 `enabled` 账号，账号之间使用间隔（可复用 Trae 的 `interval_seconds`），单个账号失败只标记该账号，最终汇总 `PARTIAL` / `ERROR`。前端复用 Trae 的账号卡片和「导入账号 / 添加账号 / 刷新状态 / 设为主号 / 编辑 / 删除」交互，产品差异只放在凭据表单字段和可用操作上。

## 分产品落地方式

### Qoder CN

推荐先做。改动最小：

1. `find_auth_dirs()` 已经返回多个目录。新增 `scan_local_accounts()`：遍历所有目录，逐个解密，按 `user.id` 去重；macOS 助手补充返回 `user`，否则按 token 去重。
2. `add_account(raw, label=None)`：接受 token，可选 `expiresAt`、`base_url`、备注名；token 通过 DPAPI 封装后落盘。
3. `run()` 增加账号循环：每个启用账号调用一次 `claim_once()`，不要共享 `_risk_cache_root`（改成按 `auth_dir` 缓存即可）。
4. `qoder_monitor()` 改为返回 `accounts[]` + `active_id`，前端渲染列表；旧的单账号字段保留一个版本兼容。
5. 没有账号库时仍回退到当前 `QODER_TOKEN` / 本机登录态，保持现有部署不变。

风险点：Qoder 的 `Cosy-Machine*` 请求头是机器级；多个账号共用同一机器标识是否触发风控，需要先用测试账号做只读验证，不能仅凭代码判断。

### WorkBuddy

改动更大，主要是凭据解密和账号资料：

1. 本地导入：读取当前 `workbuddy-desktop.info`，在导入时调用 `resolve_session()` 解出明文 token，提取 `uid` / `uin` / `enterpriseId` / `domain` / `endpoint`，按 `uid` 去重后落盘。用户切换 WorkBuddy 账号后再次点「导入账号」即可追加第二个账号。
2. 手动添加：表单至少需要 `accessToken` + `uid`；可选 `enterpriseId`、`domain`、`endpoint`、备注名。也支持直接粘贴完整 auth JSON（导入时解密）。
3. 请求：复用 `build_headers()`，但把 session 参数化，不再读取全局 `find_auth_file()`。
4. 签到 / 成长中心：`run_daily()` 已经按 headers / endpoint 工作，改成「对每个账号调用一次」即可；成长中心的缓存和任务状态要按账号隔离，不能复用全局 `growth.json`。
5. 到期与刷新：当前没有 refresh 实现。401 时将该账号标记为「需重新导入」，不要反复重试；客户端刷新 token 后，用户重新导入该账号即可更新凭据。
6. 设备：WorkBuddy 没有 Trae 那种每账号 `device_id`，多账号共享本机身份。这一点要明确写在 UI 提示里。

风险点：

- `sym-v1` 只能在有对应 WorkBuddy 客户端运行时的情况下解密；如果用户卸载 / 升级客户端，旧账号的加密凭据可能无法再解。导入时解密后重新用 DPAPI 封装，可以规避一部分问题。
- 手动 token + uid 足以调用签到接口，但企业账号可能还依赖 `enterpriseId` / `domain`，表单必须允许填写，否则会出现「代码能跑但账号不可用」。
- 成长中心的任务、补登卡、Buddy 状态都是账号级的；现有 `growth.json` 与缓存键需要迁移，否则多账号会互相覆盖。

## 建议的实施顺序

1. **抽共享层**：账号存储、DPAPI 封装、账号卡片渲染、账号级状态结构；先让 Trae 走新接口，行为不变。
2. **Qoder 多账号**：多目录扫描 + 手动 token + 账号循环；用合成数据测试去重、掩码、401、部分成功。
3. **WorkBuddy 多账号**：本机导入 + 手动 token/uid + 账号循环；单独处理成长中心缓存。
4. **UI 与文档**：两个产品的账号标题栏接入「导入账号 / 添加账号」，手动表单按产品增加字段；补风险提示。
5. **真实环境验证**：只用测试账号做只读 `status` / `doctor`，确认不会触发风控后再验证领取。

## 不建议直接照搬的点

- 不要把 Trae 的 `device_id` 逻辑套到 WorkBuddy / Qoder：它们没有等价的账号级设备号，强行伪造既不可靠也不安全。
- 不要默认把多个账号的 token 放在同一个明文文件里；必须沿用 DPAPI 封装和掩码输出。
- 不要在没有账号库迁移方案的情况下直接改 `growth.json` / dashboard 缓存键；否则升级后旧用户会看到空数据。
- 不要把多账号默认开启为「无限账号」；建议限制数量、保留启用开关，并在 UI 说明平台风控风险。

## 验证清单

- 两个本地账号导入后只出现两条记录，重复导入只更新不新增。
- 手动 token 与本地导入的同一账号按 UID / user.id 合并。
- 一个账号 401 时，其他账号仍能签到，结果为 `PARTIAL`。
- 公开接口只返回掩码，不返回 token / session / 设备原文。
- 账号删除 / 停用 / 设为主号后，历史、日历、用量只跟随主号。
- 计划任务单账号失败时，失败记录能定位到具体账号；自动恢复不会覆盖其他账号。
- WorkBuddy `sym-v1` 在客户端运行时可用 / 不可用两种情况下都有明确错误，不泄露密钥。
- Qoder macOS 的 `user` 返回补齐后，去重与资料展示均正常；未补齐时按 token 去重仍可用。
