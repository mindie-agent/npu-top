# npu-top 0.3.0

[English](npu-top-0.3.0.en.md)

本版本让监控使用 remote-dev 与 coordinator 的同一套 SSH 认证。输入一次密码后，由 remote-dev 保存凭据并尝试安装公钥；不支持公钥时继续使用保存的密码。npu-top 数据库仅保存 `auth_ref`，常规采集不安装或修复远端密钥，也不引入第二份密码存储。

批量添加界面采用掩码密码输入框，一批主机共用一个密码，提交后清空输入。逐台结果区分公钥与密码认证，并保留认证成功后登记或调度失败的事实。提交期间禁止重复提交；已有认证的显式连接检查使用新 SSH 握手，避免把缓存连接当作当前凭据仍然有效的证据。

数据库自动增加认证引用列。尚无共享配置的旧主机继续引用原监控私钥和 `known_hosts`；已有共享配置时使用共享身份与主机信任设置。旧自定义 bootstrap 命令及监控自带 askpass 实现不再使用。与 coordinator 的双向清单同步保留同一个认证引用，无法在当前用户环境解析的引用明确报告为未就绪。

共享密码默认位于 `~/.mindie/ssh/credentials.json`，是当前用户私有目录下的明文文件；POSIX 目录和文件权限为 `0700`、`0600`。各组件应运行于同一用户并使用同一共享路径，不调用系统钥匙串或要求系统解锁。原生 Windows 与 WSL 的用户主目录独立。

发布前须等待 remote-dev 0.11.0 正式 Release，然后将候选依赖范围替换为该 Release 的完整 Git 提交。mindie-diagnostics 固定为 0.5.4（`5ae643e19223f2181ecdbaa3edaf464137c4805e`）。当前本地验收为 239 项 Python 测试、60 项子测试和 6 项前端行为测试通过，另通过 lint、TypeScript 检查、前端构建与固定 benchmark 工作负载。原生 Windows PowerShell 和真实 WSL 验收仍待完成；最终发布前须补齐平台与源码 SHA 证据。正式 benchmark 报告由 Tag 流水线生成并随包交付。
