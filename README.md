# cidaren 词达人整合版

这个项目把网页版任务控制台和 Token 获取流程放到同一个本地网页中：启动程序、点击获取
Token、在 PC 微信中打开词达人学生端，鉴权验证成功后即可选择任务运行。

项目可以放在任意目录。任务接口、HTTP 请求头、配置、答题逻辑、题库、任务调度和
测试都以 `cidaren-main` 网页版为基线。桌面版只用于参考 Token 获取流程，
不使用它的界面、答题逻辑、网络请求模块、spaCy 模型或旧版 EXE。

## 功能

- 在网页中自动获取并验证 `USERTOKEN`、`ABC`、`AUTH_V` 和 `USER_AGENT`。
- 展示并运行班级任务和自学任务。
- 支持单次执行、循环到满分、批量启动班级任务、停止任务和查看日志。
- 保留本地题库以及可选的 OpenAI 兼容大模型兜底。
- Token 获取结束、取消、超时、异常退出时恢复原系统代理。

## 运行环境

项目使用名称为 `cidaren` 的 Conda 环境，要求 Python 3.12。脚本不包含用户名或 Conda
安装目录的绝对路径，可以把整个项目复制到其他 Windows 电脑和任意目录。

安装脚本会依次从当前 Conda 配置、系统 `PATH`、用户目录和系统目录中查找 Anaconda 或
Miniconda。目标电脑需要先安装其中一种；如果 `cidaren` 环境不存在，脚本会自动创建。
主要依赖如下：

| 依赖 | 版本要求 | 用途 |
| --- | --- | --- |
| Flask | `>=3.1,<4` | 本地网页和 API |
| requests | `>=2.32,<3` | 词达人及大模型请求 |
| certifi | `>=2024.8.30` | HTTPS 证书链 |
| mitmproxy | `==11.0.2` | 获取鉴权字段 |
| pytest | `>=8.3,<10` | 开发测试，可选 |

首次使用请双击 [安装依赖.bat](安装依赖.bat)。安装脚本会创建或复用 Conda 环境
`cidaren`，把可编辑安装指向脚本所在项目，并运行依赖一致性检查。

## 启动与使用

双击 [点我启动.bat](点我启动.bat)。程序默认监听：

```text
http://localhost:5001
```

启动脚本会自动打开浏览器。如果 5001 端口已占用，命令窗口会显示明确错误。

使用步骤：

1. 确认当前没有正在运行或等待循环重启的任务。
2. 点击网页顶部的“获取 Token”。
3. 在 PC 微信中打开“词达人 → 学生端”，进入任意会访问 `app.vocabgo.com` 的页面。
4. 等待网页显示“鉴权已保存并验证成功”。
5. 在任务面板选择任务，点击“启动”或“循环”。

获取成功后程序只刷新配置和任务列表，不会自动开始答题。鉴权也可以在配置中心手动填写。

## Token 获取过程

获取功能使用独立后台进程，流程如下：

1. 检查 8888 端口、运行任务和重复获取状态。
2. 在项目的 `.capture\mitmproxy` 目录生成独立证书，并检查当前 Windows 用户是否已信任。
3. 备份当前用户的 Windows 代理配置，然后临时设置为 `127.0.0.1:8888`。
4. 仅处理 `app.vocabgo.com`，其他主机由 mitmproxy 直接转发，不读取其请求内容。
5. 从同一个请求获取完整的 `USERTOKEN`、`ABC`、`AUTH_V` 和 `USER_AGENT`；字段缺失时继续等待。
6. 先停止抓取并恢复代理，再使用网页版 `Client` 的 `/Student/Main` 请求验证账号。
7. 验证通过后原子写入 `.env`；验证失败时保留原配置。

默认等待 120 秒。状态 API 和普通日志都不返回或打印完整鉴权字段。

mitmproxy 解密 HTTPS 需要客户端信任其独立 CA 证书，原理和证书说明见
[mitmproxy 官方文档](https://docs.mitmproxy.org/stable/concepts/certificates/)。

## 代理恢复

程序在更改代理之前写入 `.capture\proxy-recovery.json`。正常完成、取消、超时、后台进程异常
或 Web 程序退出时都会尝试恢复。下次启动也会检查尚未完成的恢复记录。

恢复操作只在当前代理仍是本次设置的 `127.0.0.1:8888` 时执行。如果获取过程中其他程序
修改了代理，程序不会覆盖该修改，并在 `.capture\last-proxy-recovery.json` 记录结果。

## 配置

运行配置保存在项目根目录的 `.env`：

| 字段 | 说明 |
| --- | --- |
| `USERTOKEN` | 词达人用户 Token |
| `ABC` | 词达人 `abc` 请求头 |
| `AUTH_V` | 词达人 `authorization-v` 请求头 |
| `USER_AGENT` | 获取时使用的微信 User-Agent |
| `COURSE_ID` | 自学课程 ID，默认 `CET4_v2` |
| `STUDY_GRADE` | 自学难度，默认 `2` |
| `LLM_URL` | OpenAI 兼容接口根地址 |
| `LLM_KEY` | 大模型 API Key；留空则禁用兜底 |
| `LLM_MODEL` | 大模型名称 |

配置优先级为：进程环境变量、`.env`、程序默认值。网页保存配置时使用临时文件替换，避免只
写入一半导致配置损坏。任务子进程会继承同一套配置。

## 本地 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/auth/capture/start` | 开始获取鉴权 |
| `GET` | `/api/auth/capture/status` | 返回状态和提示，不返回凭据 |
| `POST` | `/api/auth/capture/cancel` | 取消获取并恢复代理 |
| `GET/POST` | `/api/config` | 读取或保存配置 |
| `GET` | `/api/tasks` | 读取班级和自学任务 |
| `POST` | `/api/start` | 启动单个任务 |
| `POST` | `/api/start_all` | 循环启动全部未满分班级任务 |
| `POST` | `/api/stop` | 停止任务 |
| `GET` | `/api/logs` | 读取任务日志 |

鉴权获取接口只接受回环地址和本机同源页面请求。获取期间任务列表暂停刷新，所有任务启动
接口返回冲突状态。

## 测试

测试全部围绕网页版及其新增 Token 获取衔接，不使用桌面版答题代码：

```powershell
conda run -n cidaren python -m pytest -q
```

覆盖内容包括完整字段传递、字段缺失、验证失败、取消、超时、代理恢复、重复启动、任务
冲突、本机同源限制、状态脱敏、网页版 Client 传参和配置读写。

实机验收还需要在 PC 微信中打开一次词达人，以确认当前微信版本能正常信任证书、产生请求
并刷新任务列表。测试不会自动启动真实答题任务。

## 项目结构

```text
cidaren\
├─ cidaren\
│  ├─ a.py                 网页版词达人 Client、答题和题库逻辑
│  ├─ web.py               网页、任务 API 和获取控制 API
│  ├─ config.py            .env 配置管理
│  ├─ token_capture.py     获取进程、证书和代理恢复管理
│  ├─ mitm_addon.py        只读取目标主机完整鉴权字段
│  └─ bank.json            已迁移的本地题库
├─ tests\                  网页版与获取衔接测试
├─ 安装依赖.bat
├─ 点我启动.bat
└─ THIRD_PARTY_NOTICES.md
```

来源及许可证说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 和 [LICENSE](LICENSE)。
