# AGY·SWITCH

Antigravity 账号切换 + 额度监控的桌面工具。**Tauri 2 封装**（与 [zcode-switch](https://github.com/pjpv/zcode-switch) 同框架的原生窗口应用），账号管理思路参考 [Antigravity-Manager](https://github.com/lbjlaq/Antigravity-Manager)。前端为单文件 HTML，后端 Rust，嵌入本地服务承载 API。

## 使用

此仓库提供源码。先在 `src-tauri/` 下执行 `cargo build --release`，再双击生成的 `target/release/agy-switch.exe`；也可以将它复制到项目根目录使用。

- 原生窗口打开（WebView2，Win11 自带运行时），进程内嵌 `127.0.0.1` 本地服务，无浏览器
- 首次运行自动把当前 IDE 登录快照进账号库
- 端口占用时自动顺延（8791-8800）；检测到已有 AGY·SWITCH 实例则直接复用
- **浅色为默认主题**，工具条右侧 🌙/☀️ 可切换深色，选择保存在本地

| 操作 | 说明 |
| --- | --- |
| 🔑 OAuth 登录 | 打开系统浏览器授权 Google 账号后自动入库（本地回调端口 8792-8801），**支持额度查询** |
| ＋ 添加当前登录 | 收录 IDE 正在使用的账号（凭据管理器正源），无则退回 CLI 凭据文件 |
| ⇄ 切换 | 凭据写回凭据管理器（对 IDE 真实生效）+ CLI 文件层，切换前自动快照当前登录（绝不丢号） |
| ⟳ 刷新额度 | 官方分组额度：Gemini Models / Claude and GPT models × 每周 + 5 小时 |
| ▶ / ⏹ 启动/关闭 Antigravity | 单按键动态切换：未运行时显示 `▶` 点击启动；运行时显示 `⏹` 点击关闭 |
| ↻ 重启 Antigravity | 切换后需重启 IDE 生效；可在设置中勾选"切换后自动重启" |
| ✎ 重命名 | 修改账号备注（默认备注为邮箱） |
| ⤴ 导出凭据 | 原生"另存为"对话框，导出该账号的 oauth_creds.json |
| ⇪ 导入凭据 | 粘贴其他机器导出的 `oauth_creds.json` 内容 |
| ⚙ 设置 | 语言（置于首位）· 主题（浅色/深色/跟随系统）· **Antigravity 程序路径浏览与保存** · 切换后自动重启 · **自动刷新当前账号额度**（关/1/5/15 分钟，默认 5）· 开机自启动 · 关闭驻留托盘 |
| 双击账号名 | 重命名（同 ✎） |

### 额度自动刷新

后台线程按设定间隔（默认 5 分钟，对齐 Antigravity-Manager 的调度周期）**只自动刷新当前使用中的账号**；非活跃账号保持手动。启动时立即同步一次。附带收益：刷新走 token 续期链路，当前账号的 access_token 保持新鲜并同步回凭据管理器，IDE 长时间挂机也不会拿到过期 token。频率参考：Antigravity-Manager 调度器为固定 300 秒遍历全部账号；zcode-switch 无自动刷新（纯按需）。

### 托盘

右下角托盘常驻图标（AGY·SWITCH）：**左键点击显示主窗口**；右键菜单可**一键切换账号**（● 当前 / ○ 其他）、显示主窗口、退出——不开窗口也能切号。

- 设置中的「开机自启动」写入系统启动项（注册表 Run 键）
- 「关闭窗口时驻留托盘」开启后，点窗口 × 只隐藏到托盘（托盘菜单退出才真正退出）
- 界面语言（中文/English）即时切换，托盘菜单文案跟随

- 窗口尺寸/位置在关闭时自动记忆，下次启动恢复
- 深浅色主题跟随设置，原生标题栏同步变色，偏好持久化于 `~/.agy-switch/prefs.json`

## 工作原理

### OAuth 客户端配置

公开源码不内置 OAuth client ID 或 client secret。需要浏览器 OAuth 登录或续期令牌时，将 `oauth_clients.example.json` 复制为 `oauth_clients.local.json`，填写与对应客户端匹配的参数；已有私有配置请保留。账号切换功能可以直接使用已有登录凭据，无需重新 OAuth 登录。

- 桌面版优先从 exe 所在目录及其有限上级目录查找私有配置，随后检查当前工作目录及其父目录；从 `src-tauri/target/debug/` 或 `release/` 运行时可以读取项目根目录的配置
- Python 版优先读取脚本所在目录的配置，随后检查当前工作目录及其父目录
- 可以通过 `AGY_OAUTH_CONFIG` 指定配置文件路径；指定后不会退回其他文件
- 完整的环境变量对可以覆盖文件配置：`AGY_ANTIGRAVITY_CLIENT_ID` / `AGY_ANTIGRAVITY_CLIENT_SECRET`，以及 `AGY_GEMINI_CLIENT_ID` / `AGY_GEMINI_CLIENT_SECRET`
- 只设置一项环境变量或留空时，该客户端不可用；程序不会把环境变量与文件中的另一项参数混用
- 配置在进程内缓存，修改后重启工具生效。`oauth_clients.local.json` 已被 Git 忽略，请勿提交或分享此文件

缺少完整配置时，OAuth 登录和令牌续期会给出配置提示；已有凭据仍未过期时可以查询额度。

- **凭据正源（IDE）**：Antigravity 把登录凭据存在 **Windows 凭据管理器** `gemini:antigravity` 条目中（blob 为 JSON：`auth_method` / `id_token`(JWT，含邮箱) / `token`{access, refresh, expiry}）。本工具直接读写该条目——**切换对 IDE 真实生效**；收编账号时保留原始 blob（`_ide_blob`），切回时字节级还原
- **CLI 兼容层**：`~/.gemini/oauth_creds.json`（gemini CLI / 老工具使用）与 `~/.gemini/google_accounts.json` 的 `active` 字段，切换时一并写回
- **账号库**：`~/.agy-switch/accounts.json`，仅存本地，无任何遥测；所有写入均为临时文件 + 原子替换
- **注意**：IDE 刷新 token 后会重写 keyring 且不再含 `id_token`，工具按 **refresh_token**（跨刷新稳定）把 keyring 与账号库对齐；若 IDE 与 CLI 凭据不一致，界面会显示警示
- **额度 API**（daily → sandbox → prod 回退，Bearer 认证），展示与官方 IDE 一致的分组结构：
  - `v1internal:loadCodeAssist` — 解析项目与套餐档位（付费档位优先展示，如 Google AI Pro）
  - `v1internal:retrieveUserQuotaSummary` — 直接输出官方分组：**Gemini Models / Claude and GPT models × 每周 + 5 小时限额**
- **Token 刷新**：access_token 过期前 15 分钟用 refresh_token 续期；refresh_token 只能由签发它的 OAuth client 续期，按 antigravity → gemini-cli 顺序回退尝试；IDE 正在使用的账号刷新后同步写回 keyring

## 为什么有的账号查不了额度（403）

额度端点会校验 token 的签发 client：gemini-cli 等客户端签发的凭据只含基础 scope，调用 `fetchAvailableModels` 会返回 PERMISSION_DENIED——这类账号**切换不受影响**；查额度请用「＋ 添加当前登录」收录 IDE 正在使用的账号（走 IDE 自己的凭据），或 🔑 OAuth 登录重新添加。

## 构建

```bat
cd src-tauri
cargo build --release
:: 产物 target/release/agy-switch.exe（复制到根目录即可）
```

无需 Node/npm：前端是静态单文件，`tauri.conf.json` 的窗口 URL 指向进程内嵌服务。

## 其他

- `agy_switch.py` + `start.bat` 是早期网页版（本地服务 + 浏览器），仍可用作无 WebView2 环境的备选，主开发以 Tauri 版为准
- 凭据与账号库是明文 JSON（与 IDE 自身的存储方式一致），请勿把 `~/.agy-switch/` 提交到仓库或分享给他人
- 本地构建的 exe 未签名，首次运行可能触发 SmartScreen 提示（"更多信息 → 仍要运行"）
- 公开仓库不包含账号库、OAuth 凭据、个人偏好、日志、参考截图或预编译程序；`.gitignore` 已排除这些文件及构建缓存
- 公开源码仅提供空字段的 OAuth 配置模板；实际客户端参数、用户 access_token、refresh_token 和账号登录信息都留在本机
