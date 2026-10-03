# cidaren

词达人的本地网页工具。在 Windows 上启动命令窗口和浏览器，通过网页获取鉴权信息，
选择班级或自学任务运行，并管理本地词库。支持单次执行、循环到满分、停止任务、查看日志
和可选的大模型兜底。

## 安装与首次启动

准备以下环境：

- Windows，以及用于打开词达人学生端的 PC 微信。
- Anaconda 或 Miniconda；项目使用 `cidaren` 环境中的 Python 3.12。
- Git for Windows，以及 `origin` 仓库的推送权限。首次迁移和词库维护需要上传并核验备份。

在准备存放项目的目录打开 PowerShell：

```powershell
git clone https://github.com/qianjiyi/cidaren.git
cd cidaren
.\安装依赖.bat
```

[安装依赖.bat](安装依赖.bat) 会查找 Conda，创建或复用 `cidaren` 环境，将 Python 调整为
3.12，安装项目及运行依赖并运行 `pip check`。首次安装还会从 [.env.example](.env.example) 创建
`.env`；已有配置会保留。依赖及版本范围见 [pyproject.toml](pyproject.toml)，其中
mitmproxy 固定为 `11.0.2`。Conda 不在常见目录时，可先从 Anaconda Prompt 运行脚本，
或将 `CONDA_EXE` 设置为本机 `conda.exe` 的路径。

在当前仓库配置提交身份并登录 Git：

```powershell
git config --local user.name "你的GitHub用户名"
git config --local user.email "你的GitHub noreply邮箱"
git credential-manager github login
```

邮箱应填写 GitHub 账号设置中显示的完整 noreply 地址。登录账号必须能向 `origin` 推送。
仅克隆公开仓库并不会获得写入权限；没有本仓库权限时，先在自己的账号建立可写仓库或
Fork，再执行 `git remote set-url origin <你的仓库地址>`。远端地址不要包含 Token 或密码。
维护命令使用已保存的 Git 登录信息，运行时不会弹出登录窗口。

首次使用内置题库时，关闭网页和任务，再执行：

```powershell
.\词库管理.bat migrate
.\点我启动.bat
```

迁移会先上传原始输入到 `backups` 分支，再建立 `data/lexicon.sqlite3`。重复执行不会重新
导入已迁移数据。未手动迁移时，启动程序也会尝试迁移；网络、登录或推送失败会中止，
保留输入。已有运行词库的项目不需要再次迁移。

[点我启动.bat](点我启动.bat) 会自动打开浏览器，服务仅监听 `127.0.0.1:5001`。可访问
`http://127.0.0.1:5001` 或启动时打开的 `http://localhost:5001`；端口占用时命令窗口会报错。
关闭服务可在命令窗口按 `Ctrl+C`。

## 网页获取 Token 与运行任务

1. 确认没有运行中的任务或等待重启的循环任务，点击“获取 Token”。
2. 在 PC 微信中打开词达人学生端，进入会请求 `app.vocabgo.com` 的页面。
3. 等待网页提示鉴权验证成功，配置和任务列表会自动刷新。
4. 选择班级或自学任务，点击“启动”或“循环”。也可以批量启动未满分的班级循环任务。
5. 在任务面板查看日志、分数和状态，使用“停止”结束任务及后续循环。

获取成功后由用户手动启动任务。配置中心也支持手动填写鉴权字段。获取期间暂停任务列表
自动刷新并禁止启动任务；可点击“取消获取”，默认等待时间为 120 秒。

获取程序从同一请求读取完整的 `USERTOKEN`、`ABC`、`AUTH_V`，同时保存 `USER_AGENT`。
缺少鉴权字段会继续等待，避免混用不同请求的新旧字段。程序先恢复代理，再调用账号信息
接口验证；验证通过才一次性保存配置，失败时显示原因并保留原配置。

首次获取会在 `.capture/mitmproxy/` 生成项目专用证书，并检查、安装当前 Windows 用户的
证书信任。HTTPS 捕获需要微信客户端信任该证书。获取期间临时使用 `127.0.0.1:8888`
代理，仅解析目标主机的相关请求。成功、取消、超时、后台异常和服务退出均尝试恢复代理；
异常退出后的恢复记录在下次启动时检查。只有当前代理仍属于本次获取时才恢复，其他程序
期间修改的代理会保留。获取状态接口和普通日志不返回完整凭据。

## 配置

配置中心与项目根目录 `.env` 使用相同字段。优先级为：进程环境变量 → `.env` → 默认值。
网页保存采用临时文件替换；任务子进程继承更新后的配置。

| 字段 | 用途 |
| --- | --- |
| `USERTOKEN`、`ABC`、`AUTH_V` | 词达人鉴权，需完整填写 |
| `USER_AGENT` | 获取时的微信 User-Agent，传给客户端和任务进程 |
| `COURSE_ID` | 自学课程，默认 `CET4_v2` |
| `STUDY_GRADE` | 自学难度，默认 `2` |
| `LLM_URL` | OpenAI 兼容接口地址 |
| `LLM_MODEL` | 大模型名称 |
| `LLM_KEY` | API Key，留空禁用大模型兜底 |

`.env.example` 的鉴权和密钥均为空。实际 `.env` 只保存在本机，Git 不负责恢复这些配置。

## 词库查询与答案学习

运行词库位于 `data/lexicon.sqlite3`，使用 Python 内置 SQLite。内置
[cidaren/bank.json](cidaren/bank.json) 有 7612 条原始记录，是首次迁移输入，运行时不再写入。
迁移保留全部原文；当前可推导 7601 条候选，11 条答案可能截断，保留并报告问题。
候选索引会去重，其记录数量可能与原文数量不同。历史候选需要再次验证，不能直接等同于
已确认的正式答案。

查询顺序固定为：

```text
正式精确题库 → 已验证临时缓存 → 跨任务词库 → 规则 → LLM → 猜测兜底
```

跨任务查询先使用已入库知识，再尝试完整且上下文兼容的历史候选。选择题保存答案文字，
在当前选项中重新映射真实 `answer_tag`，支持选项换序和非连续标签。文字重复、答案冲突
或映射不唯一时继续回退。句子题保留语境，组词保留顺序及词数，填空区分模板、完整短语
与空格答案；未知题型只支持精确匹配。规范化保留词边界、否定、词性和词形，不进行模糊合并。

规则和大模型答案先进入待验证缓存，服务器明确判定正确后才允许入库。纠错响应经过类型
检查与答案映射，未经确认的结果继续待验证。多选题只有完整答案集合通过验证后才可入库。
阅读阶段的官方释义也进入缓存，多个词义分别保留。

缓存默认不自动过期。手动入库会在同一事务内更新正式记录、知识索引并移除对应缓存；
冲突答案保留，无法消歧时回退。服务器否定的应用关系会被停用并记录纠错，避免持续复用
已知错误。各任务读取最新提交的数据，避免内存快照覆盖其他进程的更新。

日志标记 `[精确题库]`、`[临时缓存]`、`[正式词库]`、历史候选、规则、大模型或猜测。
`[词库未命中]` 与 `[LLM回退]` 提示会解释未命中的原因。

## 缓存入库与管理脚本

双击 [词库管理.bat](词库管理.bat) 打开交互菜单，也可在项目目录执行：

| 命令 | 操作 |
| --- | --- |
| `.\词库管理.bat status` | 查看正式库、缓存、待验证和历史数据统计 |
| `.\词库管理.bat preview` | 预览新增、重复、冲突及不可转换项 |
| `.\词库管理.bat preview --limit 0` | 显示全部记录 |
| `.\词库管理.bat promote --ids 12,15` | 将指定的已验证记录入库，ID 来自预览 |
| `.\词库管理.bat promote --all` | 将全部已验证缓存入库，跳过待验证或已否定记录 |
| `.\词库管理.bat clear-cache` | 确认并备份后清理缓存，保留正式库和历史数据 |
| `.\词库管理.bat backup` | 上传当前源码及完整词库快照 |
| `.\词库管理.bat export .\词库导出.json` | 显式指定路径，手动导出版本 JSON |
| `.\词库管理.bat restore git:backups` | 恢复远端最新词库快照 |
| `.\词库管理.bat restore git:<提交SHA>` | 恢复备份分支历史中的指定快照 |
| `.\词库管理.bat restore .\词库导出.json` | 从版本 JSON 或 SQLite 文件恢复 |

CLI 等价入口为 `conda run --no-capture-output -n cidaren python -m cidaren.bank_tools`，
在后面添加相同参数即可。`export` 不指定路径时执行 Git 备份。清理和恢复默认要求输入
`YES` 确认，自动化调用可显式添加 `--yes`。首次迁移和恢复要求网页及任务全部停止。

首次迁移、入库、清理缓存和恢复现有数据库前，会先上传并核验 Git 备份。网络、登录、推送
或核验失败时取消维护，保留原数据。上传期间出现新写入，维护操作也会取消并提示重试。
入库和清理不会自动启动任务；缓存清理不影响正式记录、历史原文和错误关系停用记录。

## Git 备份与恢复

`main` 已合并运行源码、脚本、项目配置、文档、许可证、内置 `bank.json`，以及
`backup/lexicon.json` 中的完整词库快照。`backups` 继续保存后续自动及手动备份，通过普通
提交历史保留各版本。运行数据写入 `data/lexicon.sqlite3`；保存最新学习结果时仍执行
`backup`，跨电脑优先使用 `restore git:backups` 恢复最新备份。

每次完整备份包含 `backup/lexicon.json` 和 `backup/manifest.json`；首次迁移备份包含
`backup/legacy-input.json`。SQLite 备份接口生成一致快照，版本 2 JSON 保存六张业务表、
自增序号和格式版本。下载时核对清单和 SHA-256，恢复前检查结构、完整性和版本，损坏数据
会报错。恢复只替换运行词库，不切换源码，也不改本机鉴权。

日常答题写入缓存时不会逐条上传。需要保存最新学习结果时执行 `backup`；维护操作则会
自动备份。程序不自动生成本地备份文件；显式指定路径的 `export` 仍可使用。Git 自身的
对象和历史保存在 `.git/`。未登录时先运行 Git Credential Manager 登录，再重试维护。
两台电脑同时备份可能导致一次普通推送被拒绝，重试前查看最新备份，避免覆盖其他电脑的进度。

当前 GitHub 仓库公开，`backups` 中的完整词库和缓存也公开。上传排除实际 `.env`、密钥、
证书、代理恢复记录、日志、SQLite 数据库、运行锁、测试代码及产物和任务截图。运行目录
只保留运行、安装及词库管理所需文件。已有提交和标签保留，历史词库备份仍可恢复。
恢复兼容版本 1 JSON 和 SQLite 文件；版本 1 没有保存自增序号，会按现存记录重建。

## 跨电脑使用

在新电脑安装 Conda、Git，克隆 `main`，配置具有推送权限的 Git 登录，然后执行：

```powershell
git clone https://github.com/qianjiyi/cidaren.git
cd cidaren
.\安装依赖.bat
# 在此配置当前仓库的提交身份、Git 登录及可写 origin
.\词库管理.bat restore git:backups
.\点我启动.bat
```

已有远端完整快照时，先恢复再启动即可保留正式库、缓存、历史数据和验证记录；无快照时
执行首次迁移。新电脑在网页重新获取 Token，并按需填写大模型配置，证书信任由新电脑
检查。GitHub 下载的 ZIP 不含 `.git/`，无法直接完成自动备份，建议使用 Git 克隆。

也可以关闭网页和任务后，将整个项目目录（包括隐藏的 `.git/`、本机 `.env` 和 `data/`）
私下复制到另一台电脑，再运行安装脚本。脚本按自身所在目录定位文件，无需固定盘符或
用户名；Conda 环境不随项目复制。证书与代理恢复记录属于本机状态，直接复制时不带
`.capture/`，让目标电脑首次获取时重新生成。

## 运行检查

在项目目录运行：

```powershell
conda run --no-capture-output -n cidaren python -m pip check
conda run --no-capture-output -n cidaren python -m cidaren.bank_tools status
```

第一条检查依赖是否兼容，第二条校验运行词库并显示记录统计。首次迁移未完成时先执行
`migrate`；词库损坏时程序报错，不会自动创建空库覆盖。启动网页后，可通过配置中心与
任务面板检查鉴权和日志。Token 获取需要在 PC 微信中打开词达人，检查证书信任、请求
捕获和任务刷新。

## 当前目录

| 文件或目录 | 用途 |
| --- | --- |
| `cidaren/web.py`、`cidaren/_runner.py` | 网页与任务子进程 |
| `cidaren/a.py`、`cidaren/config.py` | 请求、答题流程与配置 |
| `cidaren/token_capture.py`、`cidaren/mitm_addon.py` | 鉴权获取、证书和代理恢复 |
| `cidaren/bank_store.py`、`cidaren/bank_tools.py` | SQLite 词库和管理 CLI |
| `cidaren/git_backups.py` | Git 快照、校验与下载 |
| `cidaren/bank.json` | 内置迁移输入 |
| `backup/lexicon.json`、`backup/manifest.json` | 合并时的完整词库快照及校验清单，可用于恢复 |
| `安装依赖.bat`、`点我启动.bat`、`词库管理.bat` | 安装、启动与维护入口 |
| `.env.example`、`pyproject.toml`、`.gitignore` | 配置模板、依赖与上传排除规则 |
| `data/`、`.capture/`、`.env` | 本机生成的运行数据与私有配置，不上传 |

许可证见 [LICENSE](LICENSE)，必要署名及依赖说明见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。分发时请保留这两个文件。
