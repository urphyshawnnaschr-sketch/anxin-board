# 安心看板开发与部署说明

[返回产品介绍](../README.md)

本文面向从源码运行、二次开发和制作 Windows 安装包的维护者。下列命令均从仓库根目录执行，另有说明的除外。产品场景和日常使用流程见仓库首页，微信连接见 [扫码接入说明](wechat-qr-setup.md)。

## 从源码启动

环境要求：Windows 10/11、Python 3.11 或更新版本、Node.js 20 或更新版本、Git for Windows 2.45 或更新版本。开发验证及打包脚本另外使用 PowerShell 7（`pwsh`）。

```powershell
git clone https://github.com/urphyshawnnaschr-sketch/anxin-board.git
cd anxin-board
.\Start-AnxinBoard.cmd
```

启动器会检查环境，在当前目录创建独立 Python 虚拟环境并安装项目依赖，然后启动只绑定本机回环地址的服务并打开浏览器。首次安装依赖需要网络。以后启动复用本目录已安装的依赖。

从源码使用微信截图功能时，还需在该虚拟环境中安装匹配的截图浏览器（Windows PowerShell）：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = '0'
.\.venv\Scripts\python.exe -m playwright install chromium --only-shell
Remove-Item Env:PLAYWRIGHT_BROWSERS_PATH
```

Windows 运行包构建会将匹配的浏览器纳入安装包，客户使用安装包时无需另装 Node.js、Python 或开发依赖。

从源码开发时请使用较短的仓库路径（例如 `C:\src\anxin-board`），以免浏览器可执行文件路径超过 Windows 长度限制。

请通过 `Start-AnxinBoard.cmd` 打开业务页面。它会建立一次性的本地浏览器会话；单独打开开发服务器地址，不等同于完成受保护操作所需的会话建立。

停止当前源码目录启动的进程：

```powershell
.\Stop-AnxinBoard.cmd
```

停止脚本会核对进程身份和源码目录，不会只根据端口号结束其他程序。

首次使用时，先配置项目并确认需求、功能档案和 Git 基线。日报比较的是已确认起点之后的代码变化；没有新增提交时不会把空范围伪装成新进展。已有项目的全量现状分析使用单独的进度底座流程。

## 数据与凭据

默认运行数据位于仓库外的 `%LOCALAPPDATA%\AnxinBoard\`。不要将这个目录、数据库、项目 PRD、导入的客户代码、邮件、日志或备份加入 Git。

模型和 SMTP 凭据通过产品设置流程配置，不应写入源码、测试文件、命令行参数或提交记录。真实模型请求可能计费；本仓库的离线测试使用合成数据和替身服务，测试通过不代表新的真实模型调用已获授权。

## 开发与验证

手动安装本目录依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r apps\backend\requirements.txt
npm.cmd ci --prefix apps/frontend
```

后端测试：

```powershell
.\.venv\Scripts\python.exe -E -X utf8 -m pytest tests/backend -q -ra
```

前端构建：

```powershell
npm.cmd run build --prefix apps/frontend
```

浏览器回归从 `apps/frontend` 执行，首次先安装测试浏览器：

```powershell
cd apps/frontend
npx.cmd playwright install chromium
npm.cmd run test:e2e
```

浏览器回归使用合成接口响应，验证页面交互；它与真实后端的集成验证、真实模型请求和邮件收件验收是不同层面的证据。

## Windows 打包

运行包构建要求当前目录已提交，传入的源码提交必须与当前 HEAD 一致，输出目录必须是新的目录：

```powershell
$sourceCommit = git rev-parse HEAD
pwsh -NoProfile -File installer/windows/build-runtime.ps1 -OutputRoot ../anxin-runtime -SourceCommit $sourceCommit
pwsh -NoProfile -File installer/windows/test-runtime.ps1 -PayloadRoot ../anxin-runtime/payload
```

生成安装器还需要 Inno Setup 6 和 Windows .NET Framework C# 编译器。具体参数见 `installer/windows/build-installer.ps1`。未经签名和目标客户环境验收的构建应作为测试候选，不应表述为已通过所有生产发布门。

## 使用边界

本产品不会自动向被分析仓库提交、推送或合并代码。功能状态依据所提供的代码和需求证据，不代表已经完成真实设备联调、客户验收或生产发布；代码行数也不是项目完成百分比。

重新分析和矛盾检测还有独立的模型资格门。默认资格注册表为空时不会自动放行；这与首次日报生成的准入规则不同。不得通过伪造资格、绕过确认或自动改用其他模型解除限制。

本仓库尚未附带开源许可证。仓库公开和授予开源许可证是不同事项。
