# AGY·SWITCH

Antigravity 账号切换 + 额度监控的桌面工具。前端为单文件 HTML，后端 Rust，嵌入本地服务承载 API。

## 使用

Windows x64 可直接从 [GitHub Release](https://github.com/immortalbeating/agy-switch/releases/latest) 下载：

- [安装版](https://github.com/immortalbeating/agy-switch/releases/download/v1.1.1/agy-switch-1.1.1-windows-x64-setup.exe)：运行安装程序，按向导完成安装
- [便携版](https://github.com/immortalbeating/agy-switch/releases/download/v1.1.1/agy-switch-1.1.1-windows-x64-portable.zip)：解压后运行 `agy-switch.exe`，无需安装

当前源码版本为 **1.1.1**，变更见 [更新日志](CHANGELOG.md)。

首次使用可以通过「添加当前登录」收录 IDE 已有账号。浏览器 OAuth 登录和令牌续期需要本机私有客户端配置，详见下方配置说明；发布包不提供实际客户端参数。两种版本的账号库和偏好均保存在用户目录 `~/.agy-switch/`。

- 原生窗口打开（WebView2，Win11 自带运行时），进程内嵌 `127.0.0.1` 本地服务，无浏览器
- 首次运行自动把当前 IDE 登录快照进账号库
- 本地服务优先尝试 8791-8800；整段被占用或被 Windows 保留时，自动改用系统分配的空闲端口，无需调整系统端口配置
- 桌面版通过用户目录中的进程锁和实际服务地址识别已有实例，端口变化后再次双击仍会唤醒原窗口；本地实例通信直接访问回环地址，不经过代理
- **浅色为默认主题**，工具条右侧 🌙/☀️ 可切换深色，选择保存在本地
- 深色主题使用橙金边框标记当前账号，普通账号使用清晰的灰色边框；浅色主题保留绿色选中边框

| 操作 | 说明 |
| --- | --- |
| 🔑 OAuth 登录 | 打开系统浏览器授权 Google 账号后自动入库（本地回调优先使用 8792-8801，不可用时自动选择空闲端口），**支持额度查询** |
| ＋ 添加当前登录 | 收录 IDE 正在使用的账号（凭据管理器正源），无则退回 CLI 凭据文件 |
| ⇄ 切换 | 凭据写回凭据管理器（对 IDE 真实生效）+ CLI 文件层，切换前自动快照当前登录（绝不丢号） |
| ⟳ 刷新额度 | 模型组按行、5 小时与周额度按列展示；按接口实际数据兼容额外模型组 |
| ▶ / ⏹ 启动/关闭 Antigravity | 单按键动态切换：未运行时显示 `▶` 点击启动；运行时显示 `⏹` 点击关闭 |
| ↻ 重启 Antigravity | 切换后需重启 IDE 生效；可在设置中勾选"切换后自动重启" |
| ✎ 重命名 | 修改账号备注（默认备注为邮箱） |
| ⤴ 导出凭据 | 原生"另存为"对话框，导出该账号的 oauth_creds.json |
| ⇪ 导入凭据 | 粘贴其他机器导出的 `oauth_creds.json` 内容 |
| ⚙ 设置 | 语言（置于首位）· 主题（浅色/深色/跟随系统）· **Antigravity 程序路径浏览与保存** · 切换后自动重启 · **自动刷新当前账号额度**（关/1/5/15 分钟，默认 5）· 开机自启动 · 关闭驻留托盘 |
| 双击账号名 | 重命名（同 ✎） |

### 额度自动刷新

后台线程按设定间隔（默认 5 分钟）**只自动刷新当前使用中的账号**；非活跃账号保持手动。启动时立即同步一次。刷新时会续期 token，并同步回凭据管理器。

额度区按实际返回的分组和时间窗口展开，新增的 `Gemini 4 Argon` 等组保留完整名称，和其他组使用相同的普通行；没有对应时间窗口的单元格留空。每格的重置时间与百分比同行，进度条位于下方。百分比保留一位小数，只有实际满额时才显示 `100%`，`0%` 的进度条为空。悬停数值可查看完整重置时间。旧缓存中的整数百分比会在下一次成功刷新后更新精度。

所有账号使用同一套时间显示规则：当天显示 `HH:mm`，明天显示 `明HH:mm`（中文），其他日期显示 `MM-DD`。非当前账号不会自动刷新；看到过去的重置日期或旧分组时，点击该账号刷新或「刷新全部额度」。

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
- **额度 API**（daily → sandbox → prod 回退，每个账号各自 Bearer 认证），保留官方返回的分组结构：
  - `v1internal:loadCodeAssist` — 解析项目与套餐档位（付费档位优先展示，如 Google AI Pro）
  - `v1internal:retrieveUserQuotaSummary` — 展示 Gemini、Claude / GPT 以及实际返回的 Gemini 4 Argon 等分组；按各组实际窗口显示
  - Cloud Code 会根据客户端标识筛选返回分组。旧 `vscode/1.99.0 (Antigravity/4.3.0)` 标识会遗漏有权限账号的 Argon；查询使用经本机 CLI 1.4.7 对照验证的 `antigravity/cli/... (aidev_client; ...; auth_method=consumer)` 格式，无需启动 CLI 或切换当前登录账号
- **Token 刷新**：access_token 过期前 15 分钟用 refresh_token 续期；refresh_token 只能由签发它的 OAuth client 续期，按 antigravity → gemini-cli 顺序回退尝试；IDE 正在使用的账号刷新后同步写回 keyring

## 为什么有的账号查不了额度（403）

额度端点会校验 token 的签发 client：gemini-cli 等客户端签发的凭据只含基础 scope，调用 `fetchAvailableModels` 会返回 PERMISSION_DENIED——这类账号**切换不受影响**；查额度请用「＋ 添加当前登录」收录 IDE 正在使用的账号（走 IDE 自己的凭据），或 🔑 OAuth 登录重新添加。

## 构建

需要 Rust 1.89 或更新版本。旧版 Python 入口的默认服务和 OAuth 回调也支持自动端口回退；显式传入 `--port` 时使用指定端口，`--port 0` 表示由系统分配。Python 备用服务使用同一个账号库，请只运行一个实例，并在使用它时退出桌面版。

构建单个程序：

```bat
cd src-tauri
cargo build --release
:: 产物 target/release/agy-switch.exe（复制到根目录即可）
```

构建 Windows NSIS 安装包需要 Tauri CLI：

```bat
cargo install tauri-cli --version "^2.0.0" --locked
cargo tauri build --bundles nsis
:: 安装包位于 src-tauri/target/release/bundle/nsis/
```

无需 Node/npm：前端是静态单文件，`tauri.conf.json` 的窗口 URL 指向进程内嵌服务。发布包还会移除本机用户名路径，并排除私有配置和构建调试文件。

### 本地验证

```bat
cd src-tauri
cargo test --locked
cargo build --release --locked
target\release\agy-switch.exe --startup-check C:\path\to\new-isolated-check-dir
```

`--startup-check` 要求一个尚不存在、父目录已存在的隔离目录。在创建窗口前关闭可见性、焦点和可聚焦状态，使用真实 WebView2 加载本地页面，完成后自动退出；不创建托盘、不读取真实账号或同步登录凭据。成功日志位于该目录下的 `.agy-switch/app.log`，包含 `STARTUP_CHECK_OK`。它验证启动链路，不代表截图或人工视觉验收。

Python 端口回归测试：`python -m unittest discover -s tests -p test_port_binding.py -v`。

额度界面回归测试：`python -m unittest discover -s tests -p test_quota_ui.py -v`（需要 Python Playwright 及其 Chromium）。测试在独立浏览器上下文中加载真实前端，用本地虚构账号响应替代接口，覆盖两组/三组、仅周额度、空白窗口、0%/小数、中英文和深浅色窄窗口；不访问真实账号库或控制系统光标。

## 其他

- `agy_switch.py` + `start.bat` 是早期网页版（本地服务 + 浏览器），仍可用作无 WebView2 环境的备选，主开发以 Tauri 版为准
- 凭据与账号库是明文 JSON（与 IDE 自身的存储方式一致），请勿把 `~/.agy-switch/` 提交到仓库或分享给他人
- 本地构建的 exe 未签名，首次运行可能触发 SmartScreen 提示（"更多信息 → 仍要运行"）
- 公开仓库不包含账号库、OAuth 凭据、个人偏好、日志、本机截图或预编译程序；`.gitignore` 已排除这些文件及构建缓存
- 公开源码仅提供空字段的 OAuth 配置模板；实际客户端参数、用户 access_token、refresh_token 和账号登录信息都留在本机
