# colab-dfs-ext —— google-colab-cli 的零浏览器 Drive 挂载与多账户扩展

> English | [中文](README.zh-CN.md)

为 `colab` CLI 增加**无浏览器 Google Drive 挂载（`colab dfs`）**、**隔离式多账户管理（`colab account`、`-a / --account`）**以及**增强版 Agent 指引（`colab skill`）**，
**不改动 google-colab-cli 的任何文件**：colab-cli 升级不受影响、无需重新集成，
最坏情况一条命令修复。

```
# 零浏览器 Drive 挂载
colab dfs mount   [-a ACCT] [--drive-account DRV] [-s SESSION] [path]
colab dfs umount  [-a ACCT] [-s SESSION] [path]
colab dfs status  [-a ACCT] [--drive-account DRV] [-s SESSION] [path]
colab dfs consent [-a ACCT] [--flavor dfs|sdk|custom] [--url-only | --code CODE]
colab dfs show | token | clear [-a ACCT] [--drive-account DRV]
colab dfs doctor  [-a ACCT]             # 体检：注入 / 多账户 / 凭据 / 修复指引
colab dfs install                       # 重新注入本扩展（升级后如果扩展消失就跑这个）

# 多账户管理（`acct` 为 `account` 的简短别名）
colab -a <account> <任意命令>           # 按配置名或邮箱指定账户（也可设置 COLAB_ACCOUNT）
colab account list [--json]             # 离线列表（不会因系统钥匙串加锁而阻塞）
colab account current [--json]          # 查看当前解析出的账户
colab account add <name> [--email ...]  # 注册并登录（支持 OAuth2 与 --auth adc / --adc-file）
colab account login [<name>] [--url-only | --code CODE]   # 交互式或无头两步 PKCE 登录
colab account use <name>                # 切换默认活跃账户
colab account status [--json]           # 并发探测所有账户 CCU 余额、消耗速率、会话与 Drive 状态
colab account set-drive <storage> [-a <compute> | --default | --clear]  # 跨账户共享 Drive 绑定
colab account rename <old> <new>        # 重命名账户并迁移会话/历史/Token
colab account remove <name> [--force]   # 删除账户及其隔离目录
colab sessions --all [--json]           # 汇总所有账户下的活跃会话
colab usage --all [--json]              # 汇总所有账户的 CCU 配额
colab skill                             # 动态增强版 SKILL.md（自动附带 dfs 与多账户说明）
```

所有子命令接受 `-- -任意参数` 透传给底层工具，本扩展没暴露的 flag 依然可用。

## 一键安装

```bash
curl -fsSL https://raw.githubusercontent.com/deluxebear/colab-dfs-ext/main/install.sh | sh
```

（wget 亦可：`wget -qO- ... | sh`。脚本只做三件事：下载仓库 tarball → 解压到临时目录 →
跑 `python3 install.py`，最后打印后续步骤；可用 `COLAB_DFS_REPO` / `COLAB_DFS_REF` 覆盖来源。）

## 环境要求

- Python 3.8+（`install.py` 与扩展本体均为纯标准库）
- 已安装并可用 [`google-colab-cli`](https://pypi.org/project/google-colab-cli/)（`uv tool install google-colab-cli`），即 `colab` 命令
- 一个已连接的 colab 会话（`colab connect -s <session>`）
- Colab 运行时自带 `/opt/google/drive`（DriveFS），无需自行上传

## 使用步骤

### 第 1 步 · 安装扩展

一键安装（见上）或手动安装：

```bash
git clone https://github.com/deluxebear/colab-dfs-ext
cd colab-dfs-ext            # 即本目录
python3 install.py          # 任意 python3 均可
colab dfs doctor            # 验证注入成功
```

`install.py` 做两件事：

1. 把扩展源码拷到 `~/.colab-dfs-ext/`（**在 colab-cli 的 venv 之外**，升级永不触碰）；
2. 往 colab-cli 环境的 site-packages 里写**唯一的一个文件** `colab_dfs.pth`（~600 字节）。

常用安装参数：

```bash
python3 install.py --status        # 查看 .pth / 源码 / colab_cli 各自的位置
python3 install.py --link          # 开发模式：符号链接本仓库，改代码即生效
python3 install.py --site-dir DIR  # 指定目标 site-packages
```

### 第 2 步 · 一次性授权（只需做一次）

```bash
colab dfs consent            # 浏览器弹出 Google 同意页 -> 允许
colab dfs show               # 查看已存凭据（refresh token / GaiaId / email）
```

默认使用逆向出来的 DriveFS 内置客户端（flavor `dfs`，Google 自家
“Google Drive for desktop”，已验证应用），localhost 回调自动接住，无需复制授权码。
其他 flavor：`--flavor sdk`（colab-cli 自带客户端，headless 可用但易被 Google 拦）、
`--flavor custom --client-id ... --client-secret ...`（自己的 GCP 项目，长期最稳）。
已同意过的机器可以 `colab dfs consent --import`，完全无浏览器。

DriveFS 凭据存于 `~/.config/colab-cli/accounts/<account>/dfs_credentials.json`（首次运行会自动且**无损**地从旧版 `~/.config/colab-cli/dfs_credentials.json` 纳管到 `default`），之后所有挂载零浏览器。

### 第 3 步 · 日常挂载

```bash
colab connect -s test        # 连上 colab 会话（如未连接）
colab dfs mount -s test      # 挂到 /content/drive，几秒完成
colab dfs status -s test     # 查看挂载状态
colab dfs umount -s test     # 卸载

colab dfs mount -s test --force --purge-state   # 重挂：清旧进程/旧目录/旧本地库
colab dfs mount -s test --dry-run               # 只打印配置和远端 payload
colab dfs mount -s test --readonly              # 只读挂载
```

### 多账户工作流与跨账户共享 Drive

首次使用时，既有的 `~/.config/colab-cli/{sessions.json,history/,dfs_credentials.json}` 与系统钥匙串 `oauth_token` 会**无损自动纳管**为 `default` 配置（保留原文件不删除，可随时用 `colab account rename default personal` 改名）。

每个账户的隔离目录位于 `~/.config/colab-cli/accounts/<name>/`（含 `sessions.json`、`history/`、`dfs_credentials.json`、`dfs_pending.json`、`oauth_pending.json`，以及钥匙串不可用时的 `0o600` 降级文件 `oauth_token.json`），系统钥匙串槽位为 `oauth_token:<name>`，注册表位于 `~/.config/colab-cli/accounts.json`。

```bash
# 添加账户（默认同时完成 Colab 登录与 DriveFS 同意）
colab account add work --email me@work.com
colab account add gpu2 --no-dfs --drive-account work   # 多算力号共享同一个 Drive 存储号

# 无头环境 / SSH / Agent 两步授权（Colab OAuth 与 DriveFS 均支持）
colab account login work --url-only
colab account login work --code <粘贴授权码>
colab -a work dfs consent --flavor sdk --url-only
colab -a work dfs consent --code <粘贴授权码>

# 通过 -a、COLAB_ACCOUNT、切换默认账户，或按已有会话名自动路由
colab -a work new -g T4 -s gpu-a             # 在 `work` 下新建会话
export COLAB_ACCOUNT=work                    # Shell 会话级覆盖
colab account use personal                   # 切换全局默认账户
colab exec -s gpu-a -- nvidia-smi            # 自动把已有会话 gpu-a 路由到 `work`（提示走 stderr）
colab url -s gpu-a                           # 自动附加 authuser=<email> 方便浏览器打开

# 跨账户并发查看与共享 Drive
colab account status                         # 并发查看所有账户的 CCU、消耗速率、会话与 Drive
colab sessions --all [--json]                # 汇总所有账户下的活跃会话
colab usage --all [--json]                   # 汇总所有账户的 CCU 配额
colab account set-drive main -a gpu2         # 将 gpu2 的 Drive 持久绑定到 main
colab -a gpu2 dfs mount -s gpu-a --drive-account main   # 单次临时覆盖挂载目标 Drive
```

供 AI Agent 使用时，`colab skill` / `colab SKILL` 会动态把 `colab dfs` 与 `colab account` 用法注入输出结果中（并保留合法的 YAML frontmatter）。

### 原生 `colab drivemount` 默认增强

官方命令 `colab drivemount` 现已默认接入零浏览器 DriveFS 与多账户体系，直接支持
`-a/--account`、`--drive-account`、`--readonly`、`--force`、`--purge-state`、
`--timeout`、`--dry-run` 以及按会话自动路由：

```bash
colab drivemount -s test                             # 默认走零浏览器 DriveFS，自动路由所属账户
colab drivemount -s test --drive-account storage     # 挂载其他账户的 Drive
colab drivemount -s test --readonly --no-fallback    # 只读挂载；无凭据时直接报错不回落交互提示
colab drivemount -s test --upstream                  # 强制走官方原版浏览器交互流程
COLAB_DFS_DEFAULT=0 colab drivemount -s test         # 通过环境变量禁用零浏览器增强
```

当目标账户尚无 DriveFS 凭据（且未传 `--no-fallback` 或 DriveFS 专属参数）时，会自动提示运行
`colab dfs consent` 并回落到原版 `drivemount`（在弹出授权链接前提醒目标账号）。

## 升级共存（实测）

| 场景 | 结果 |
|---|---|
| `uv tool install google-colab-cli --reinstall`（重建工具环境） | `.pth` **幸存**（uv 只重装自己的包，不清理外来文件），`colab dfs` 直接可用 |
| `colab update --install` | 内部会跑 `uv tool install -U`；扩展注册时包装了 `update` 命令，升级完成后自动检查并重新接线（`[colab-dfs] extension still wired: ...`） |
| 极端情况（换 Python 版本、手动清 venv） | `colab dfs` 消失，但 `~/.colab-dfs-ext` 还在：`python3 ~/.colab-dfs-ext/install.py` 一条命令恢复 |

## 卸载

```bash
python3 install.py --uninstall          # 摘掉 .pth
rm -rf ~/.colab-dfs-ext                 # （可选）删掉扩展源码副本
```

## 排障

```bash
colab dfs doctor                # 体检：注入/凭据/会话，附修复指引
COLAB_DFS_DEBUG=1 colab dfs …   # 加载器/注入的详细诊断
```

- `colab dfs` 命令消失 → `python3 ~/.colab-dfs-ext/install.py` 重新接线。
- 改了本仓库代码但行为没变 → `~/.colab-dfs-ext` 里是安装时的快照，重跑
  `python3 install.py`（或改用 `--link`），`source_dir()` 的查找顺序是
  环境变量 `COLAB_DFS_EXT` → 当前代码位置 → `~/.colab-dfs-ext`。

## 注入原理（为什么升级不会破坏它）

- colab-cli 是 Typer 应用，`colab_cli.cli` 暴露模块级 `app` 对象，且官方命令模块本身就是
  `register(app)` 约定 —— 扩展通过非侵入式钩子隔离认证与状态，并往同一个 `app` 上挂载
  `account`（`acct`）与 `dfs` 子命令组，**零侵入**。
- `.pth` 是 site.py 唯一会自动执行的钩子：一行 `import os, sys; exec(compile("try: ... import
  colab_dfs_loader ..."))`，把 `~/.colab-dfs-ext` 加进 `sys.path` 并急切 import loader；
  loader 调 `colab_dfs_ext.register(app)`。启动开销实测 **≈0**（323ms vs 324ms：colab 本来就要
  import 这些模块）。
- 防御性：snippet 全部包在 try/except 里，扩展缺失/损坏时静默跳过，**永远不会弄坏 `colab` 本身**
  （`COLAB_DFS_DEBUG=1` 可看跳过原因）。

## 已知实现细节 / 坑

- `remote.exec_code` 用 colab_cli 自己的 `ColabRuntime.execute_code()`（无子进程、无临时文件），
  **必须在 finally 里 `runtime.stop()`**——否则内核客户端的非 daemon 线程会把 `colab` 进程吊住
  不退出（实测症状：结果都打印了但命令不返回）。
- 各账户的 DriveFS 凭据独立存于 `~/.config/colab-cli/accounts/<account>/dfs_credentials.json`
  （旧版 `~/.config/colab-cli/dfs_credentials.json` 会自动无损纳管到 `default` 并保留原文件）。
- 背景技术细节（DriveFS 的凭据契约、metadata shim、三条去浏览器方案）见上层仓库
  `colab-drivemount-re/docs/REVERSE.md`。

## 目录结构

```
colab-dfs-ext/
├── install.sh               # curl | sh 一键安装引导脚本（POSIX sh）
├── install.py               # 安装/修复/卸载/查看状态（纯标准库）
├── colab_dfs_loader.py      # .pth 启动钩子加载的 loader
├── tests/
│   └── test_multi_account.py # 隔离式多账户、DriveFS 与 skill 单元测试
└── colab_dfs_ext/           # 扩展本体（会被拷贝/链接到 ~/.colab-dfs-ext/）
    ├── __init__.py          # register(app) 入口
    ├── inject.py            # .pth 写入与状态管理
    ├── accounts.py          # 多账户注册表、路径隔离、平滑迁移、钥匙串/文件后端
    ├── account_commands.py  # `colab account` 子命令（typer）
    ├── cli_hooks.py         # 全局 -a/--account、认证/状态隔离、--all 与 skill 钩子
    ├── skill_doc.py         # 动态增强 `colab skill` / `colab SKILL` 输出
    ├── commands.py          # `colab dfs` 各子命令（typer）
    ├── dfs_creds.py         # OAuth 同意 / 凭据存取
    ├── drive_mount.py       # 远端挂载脚本（metadata shim + DriveFS）
    └── remote.py            # 通过 colab_cli API 在运行时内执行代码
```
