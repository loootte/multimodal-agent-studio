"""工具引擎（#89 从 poc/tools 搬来）：只从 toolpacks/*/tool.json 清单注册、校验、安装、沙箱和 MCP 投影。

工具代码不在这里：内置工具在 toolpacks/builtin/<名字>/tool.py（host 工具在 apps/web/chat_run.py），
第三方包在 toolpacks/installed/<名字>/。离线检查：python -m services.tools.check。
"""
