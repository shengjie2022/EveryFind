# 本地文档内容检索工具
- 职责：保留原应用用途，独立维护源码。
- 目录：原业务源码、工具和演示/夹具；docs/index.html 为人类修复说明；ai-tasks 为交接。
- 启动：推荐 Python 3.12。Windows 双击“启动检索.bat”；macOS 双击“启动检索.command”。首次联网安装依赖，后续本地浏览器使用。也可手动建立 .venv，安装 requirements.txt，再运行 run.py。
- 日志：不新增后台日志文件，应用向终端/浏览器报告状态；临时验证资料放本服务 .review/ 且不提交。
- 依赖：见本服务 requirements.txt/package.json（如适用）；无其他本仓库服务依赖。
- 部署：解压交付包后按 docs/index.html 启动，未发布公网。
- 回滚：保留原 RAR；先备份个人数据后关闭程序，恢复原目录。账本应通过标准 JSON 备份迁移，不能直接让旧版读取新版内部存储。
- 当前交付为修复源码包，没有重建 Windows EXE。Windows 批处理、PowerShell 打包和旧版 Office 转换须在 Windows 实机确认；macOS 已完成源码运行验证。Python 3.13+ 本版不提供 OCR。云端模型未使用真实 API Key 做在线验证。新包不含原索引和个人配置，原始 RAR 保留。
