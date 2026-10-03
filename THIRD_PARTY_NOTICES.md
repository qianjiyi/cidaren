# 来源说明

本整合项目包含并修改了以下本地项目的代码：

- `D:\cidaren\cidaren-main`：网页控制台、任务执行、配置和本地题库。
- `D:\cidaren\Easy_Cidaren-master`：仅参考 Token 获取的交互流程和临时代理切换方式。

任务接口、HTTP 请求头、鉴权校验、配置、调度及测试均以 `cidaren-main` 的网页版实现为
基线，不采用桌面版的答题网络层。`Easy_Cidaren-master` 随附 GNU General Public License
v3，许可证全文保存在本项目的 `LICENSE`。本项目没有复制原桌面界面、spaCy 模型、旧版
Token 获取 EXE 或日志。

mitmproxy 是独立的第三方依赖，其许可证和版权信息随安装包提供。
