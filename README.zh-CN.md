# colab-ext —— google-colab-cli 的多账户、零浏览器 Drive 与 Agent Skill 扩展

> [English](README.md) | 中文

*（原名 `colab-dfs-ext`；旧版 `colab dfs ...` 命令与 `COLAB_DFS_*` 环境变量保持 100% 向后兼容。）*

为 `colab` CLI 增加**隔离式多账户管理（`colab account`、`-a / --account`）**、**无浏览器 Google Drive 挂载（`colab drivemount` 与 `colab drive`）**、**扩展管理（`colab ext`）**以及**增强版 Agent 指引（`colab skill`）**，
**不改动 google-colab-cli 的任何文件**：colab-cli 升级不受影响、无需重新集成，
最坏情况一条命令修复。

```
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

# 零浏览器 Drive 挂载（原生 `drivemount` + `drive`；`dfs` 为兼容别名）
colab drivemount [-a ACCT] [--drive-account DRV] [-s SESSION] [path]
colab drive mount   [-a ACCT] [--drive-account DRV] [-s SESSION] [path]
colab drive umount  [-a ACCT] [-s SESSION] [path]
colab drive status  [-a ACCT] [--drive-account DRV] [-s SESSION] [path]
colab drive consent [-a ACCT] [--flavor dfs|sdk|custom] [--url-only | --code CODE]
colab drive show | token | clear [-a ACCT] [--drive-account DRV]

# 扩展自检与 AI Agent 指引
colab ext doctor [-a ACCT]              # 体检：注入 / 多账户 / 凭据 / 修复指引
colab ext install                       # 重新注入 `colab_ext.pth`（升级后如扩展消失运行这个）
colab ext version                       # 打印扩展版本与路径 JSON
colab skill                             # 动态增强版 SKILL.md（自动附带多账户与零浏览器 Drive 说明）
```

所有子命令接受 `-- -任意参数` 透传给底层工具，本扩展没暴露的 flag 依然可用。

## 一键安装

```bash
curl -fsSL https://raw.githubusercontent.com/deluxebear/colab-ext/main/install.sh | sh
```

（wget 亦可：`wget -qO- ... | sh`。脚本下载仓库 tarball → 解压到临时目录 →
跑 `python3 install.py`，若 `deluxebear/colab-ext` 尚未就绪会自动回落 `deluxebear/colab-dfs-ext`；
可用 `COLAB_EXT_REPO` / `COLAB_EXT_REF` 覆盖来源。）

## 环境要求

- Python 3.8+（`install.py` 与核心模块均为纯标准库）
- 已安装并可用 [`google-colab-cli`](https://pypi.org/project/google-colab-cli/)（`uv tool install google-colab-cli`），即 `colab` 命令
- 一个已连接的 colab 会话（`colab connect -s <session>`）
- Colab 运行时自带 `/opt/google/drive`（DriveFS），无需自行上传

## 使用步骤

### 第 1 步 · 安装扩展

一键安装（见上）或手动安装：

```bash
git clone https://github.com/deluxebear/colab-ext
cd colab-ext                # 或你的仓库目录
python3 install.py          # 任意 python3 均可
colab ext doctor            # 验证注入成功
```

`install.py` 做两件事：

1. 把扩展源码拷到 `~/.colab-ext/`（**在 colab-cli 的 venv 之外**，升级永不触碰），并自动清理旧版 `~/.colab-dfs-ext/`；
2. 往 colab-cli 环境的 site-packages 里写**唯一的一个文件** `colab_ext.pth`（~600 字节），同时自动删除遗留的 `colab_dfs.pth`。

常用安装参数：

```bash
python3 install.py --status        # 查看 .pth / 源码 / colab_cli 各自的位置
python3 install.py --link          # 开发模式：符号链接本仓库，改代码即生效
python3 install.py --site-dir DIR  # 指定目标 site-packages
```

### 第 2 步 · 一次性授权（只需做一次）

```bash
colab drive consent          # 浏览器弹出 Google 同意页 -> 允许
colab drive show             # 查看已存凭据（refresh token / GaiaId / email）
```

默认使用逆向出来的 DriveFS 内置客户端（flavor `dfs`，Google 自家
“Google Drive for desktop”，已验证应用），localhost 回调自动接住，无需复制授权码。
其他 flavor：`--flavor sdk`（colab-cli 自带客户端，headless 可用但易被 Google 拦）、
`--flavor custom --client-id ... --client-secret ...`（自己的 GCP 项目，长期最稳）。
已同意过的机器可以 `colab drive consent --import`，完全无浏览器。

DriveFS 凭据存于 `~/.config/colab-cli/accounts/<account>/dfs_credentials.json`（首次运行会自动且**无损**地从旧版 `~/.config/colab-cli/dfs_credentials.json` 纳管到 `default`），之后所有挂载零浏览器。

### 第 3 步 · 日常挂载

```bash
colab connect -s test        # 连上 colab 会话（如未连接）
colab drivemount -s test     # 零浏览器挂到 /content/drive，几秒完成
colab drive status -s test   # 查看挂载状态
colab drive umount -s test   # 卸载

colab drivemount -s test --force --purge-state  # 重挂：清旧进程/旧目录/旧本地库
colab drive mount -s test --dry-run             # 只打印配置和远端 payload
colab drivemount -s test --readonly             # 只读挂载
```

### 原生 `colab drivemount` 默认增强

官方命令 `colab drivemount` 现已默认接入零浏览器 DriveFS 与多账户体系，直接支持
`-a/--account`、`--drive-account`、`--readonly`、`--force`、`--purge-state`、
`--timeout`、`--dry-run` 以及按会话自动路由：

```bash
colab drivemount -s test                             # 默认走零浏览器 DriveFS，自动路由所属账户
colab drivemount -s test --drive-account storage     # 挂载其他账户的 Drive
colab drivemount -s test --readonly --no-fallback    # 只读挂载；无凭据时直接报错不回落交互提示
colab drivemount -s test --upstream                  # 强制走官方原版浏览器交互流程
COLAB_DRIVE_DEFAULT=0 colab drivemount -s test       # 通过环境变量禁用零浏览器增强
```

当目标账户尚无 DriveFS 凭据（且未传 `--no-fallback` 或 DriveFS 专属参数）时，会自动提示运行
`colab drive consent` 并回落到原版 `drivemount`（在弹出授权链接前提醒目标账号）。

### 多账户工作流与跨账户共享 Drive

首次使用时，既有的 `~/.config/colab-cli/{sessions.json,history/,dfs_credentials.json}` 与系统钥匙串 `oauth_token` 会**无损自动纳管**为 `default` 配置（保留原文件不删除，可随时用 `colab account rename default personal` 改名）。

每个账户的隔离目录位于 `~/.config/colab-cli/accounts/<name>/`（含 `sessions.json`、`history/`、`dfs_credentials.json`、`dfs_pending.json`、`oauth_pending.json`，以及钥匙串不可用时的 `0o600` 降级文件 `oauth_token.json`），系统钥匙串槽位为 `oauth_token:<name>`，注册表位于 `~/.config/colab-cli/accounts.json`。

```bash
# 添加账户（默认同时完成 Colab 登录与 DriveFS 同意）
colab account add work --email me@work.com
colab account add gpu2 --no-drive --drive-account work   # 多算力号共享同一个 Drive 存储号

# 无头环境 / SSH / Agent 两步授权（Colab OAuth 与 DriveFS 均支持）
colab account login work --url-only
colab account login work --code <粘贴授权码>
colab -a work drive consent --flavor sdk --url-only
colab -a work drive consent --code <粘贴授权码>

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
colab -a gpu2 drivemount -s gpu-a --drive-account main   # 单次临时覆盖挂载目标 Drive
```

供 AI Agent 使用时，`colab skill` / `colab SKILL` 会动态把 `colab-ext`（`colab drivemount`、`colab drive`、`colab account` 与 `colab ext`）用法注入输出结果中（并保留合法的 YAML frontmatter）。

## 环境变量（支持旧版兼容）

| 变量名 | 作用 | 旧版兼容名 |
|---|---|---|
| `COLAB_ACCOUNT` | 覆盖默认账户配置名或邮箱 | — |
| `COLAB_DRIVE_ACCOUNT` | 指定提供 Drive 凭据的存储账户 | `COLAB_DFS_ACCOUNT` |
| `COLAB_DRIVE_DEFAULT` | 设为 `0` 时关闭 `drivemount` 零浏览器默认增强 | `COLAB_DFS_DEFAULT` |
| `COLAB_EXT_SOURCE` | 覆盖扩展源码目录查找 | `COLAB_DFS_EXT` |
| `COLAB_EXT_DEBUG` | 设为 `1` 输出加载器与注册诊断信息 | `COLAB_DFS_DEBUG` |
| `COLAB_EXT_REPO` / `COLAB_EXT_REF` | 覆盖 `install.sh` 拉取的仓库/分支 | `COLAB_DFS_REPO` / `COLAB_DFS_REF` |

## 升级共存（实测）

| 场景 | 结果 |
|---|---|
| `uv tool install google-colab-cli --reinstall`（重建工具环境） | `.pth` **幸存**（uv 只重装自己的包，不清理外来文件），扩展命令直接可用 |
| `colab update --install` | 内部会跑 `uv tool install -U`；扩展注册时包装了 `update` 命令，升级完成后自动检查并重新接线（`[colab-ext] extension still wired: ...`） |
| 极端情况（换 Python 版本、手动清 venv） | 扩展命令消失，但 `~/.colab-ext` 还在：`python3 ~/.colab-ext/install.py` 一条命令恢复 |

## 卸载

```bash
python3 install.py --uninstall          # 摘掉 colab_ext.pth（及遗留旧钩子）
rm -rf ~/.colab-ext                     # （可选）删掉扩展源码副本
```

## 排障

```bash
colab ext doctor                # 体检：注入/多账户/凭据，附修复指引
COLAB_EXT_DEBUG=1 colab ext doctor   # 加载器/注入的详细诊断
```

- 扩展命令消失 → `python3 ~/.colab-ext/install.py` 重新接线。
- 改了本仓库代码但行为没变 → `~/.colab-ext` 里是安装时的快照，重跑
  `python3 install.py`（或改用 `--link`），`source_dir()` 的查找顺序是
  环境变量 `COLAB_EXT_SOURCE`（`COLAB_DFS_EXT`）→ 当前代码位置 → `~/.colab-ext`。

## 注入原理（为什么升级不会破坏它）

- colab-cli 是 Typer 应用，`colab_cli.cli` 暴露模块级 `app` 对象，且官方命令模块本身就是
  `register(app)` 约定 —— 扩展通过非侵入式钩子隔离认证与状态，并往同一个 `app` 上挂载
  `account`（`acct`）、`drive`（兼容别名 `dfs`）与 `ext` 子命令组，**零侵入**。
- `.pth` 是 site.py 唯一会自动执行的钩子：一行 `import os, sys; exec(compile("try: ... import
  colab_ext_loader ..."))`，把 `~/.colab-ext` 加进 `sys.path` 并急切 import loader；
  loader 调 `colab_ext.register(app)`。
- 防御性：snippet 全部包在 try/except 里，扩展缺失/损坏时静默跳过，**永远不会弄坏 `colab` 本身**
  （`COLAB_EXT_DEBUG=1` 可看跳过原因）。

## 已知实现细节 / 坑

- `remote.exec_code` 用 colab_cli 自己的 `ColabRuntime.execute_code()`（无子进程、无临时文件），
  **必须在 finally 里 `runtime.stop()`**——否则内核客户端的非 daemon 线程会把 `colab` 进程吊住
  不退出。
- 各账户的 DriveFS 凭据独立存于 `~/.config/colab-cli/accounts/<account>/dfs_credentials.json`
  （旧版 `~/.config/colab-cli/dfs_credentials.json` 会自动无损纳管到 `default` 并保留原文件）。

## 目录结构

```
colab-ext/
├── install.sh               # curl | sh 一键安装引导脚本（POSIX sh）
├── install.py               # 安装/修复/卸载/查看状态（纯标准库）
├── colab_ext_loader.py      # .pth 启动钩子加载的 loader
├── tests/
│   └── test_colab_ext.py    # 隔离式多账户、DriveFS、drivemount 与 skill 单元测试
└── colab_ext/               # 扩展本体（会被拷贝/链接到 ~/.colab-ext/）
    ├── __init__.py          # register(app) 入口
    ├── inject.py            # colab_ext.pth 写入、旧钩子清理与状态管理
    ├── accounts.py          # 多账户注册表、路径隔离、平滑迁移、钥匙串/文件后端
    ├── account_commands.py  # `colab account` 子命令（typer）
    ├── cli_hooks.py         # 全局 -a/--account、认证/状态隔离、--all、drivemount 与 skill 钩子
    ├── skill_doc.py         # 动态增强 `colab skill` / `colab SKILL` 输出
    ├── commands.py          # `colab drive`（`dfs`）与 `colab ext` 子命令（typer）
    ├── dfs_creds.py         # OAuth 同意 / 凭据存取
    ├── drive_mount.py       # 远端挂载脚本（metadata shim + DriveFS）
    └── remote.py            # 通过 colab_cli API 在运行时内执行代码
```
