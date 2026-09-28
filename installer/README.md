# Windows 构建与交付

运行包包含后端可执行程序、已构建的前端和恢复工具，构建与运行数据目录彼此分离。

在 Windows 上准备 Python 3.11+、Node.js 20+、Git for Windows 2.45+ 和 PowerShell 7。安装器还需要 Inno Setup 6 和 Windows .NET Framework C# 编译器。

在仓库根目录、已提交的精确 HEAD 上执行：

```powershell
$sourceCommit = git rev-parse HEAD
pwsh -NoProfile -File installer/windows/build-runtime.ps1 -OutputRoot ../anxin-runtime -SourceCommit $sourceCommit
pwsh -NoProfile -File installer/windows/test-runtime.ps1 -PayloadRoot ../anxin-runtime/payload
pwsh -NoProfile -File installer/windows/build-installer.ps1 -PayloadRoot ../anxin-runtime/payload -OutputRoot ../anxin-installer -ISCCPath 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
```

两个输出目录必须尚不存在。检查每步退出码，保留源码提交、payload 清单和安装器 SHA256。打包测试包含独立临时数据、备份恢复和恢复后启动；不能用源码环境的单元测试代替这些检查。

安装器按当前 Windows 用户安装。默认运行数据在 `%LOCALAPPDATA%\AnxinBoard\`，安装及卸载不得将这个数据目录当作可丢弃的构建目录。升级、恢复和停止运行程序使用产品提供的受保护流程。

本仓库的安装器构建生成测试候选，不会因为构建成功就自动满足正式发布条件。Authenticode 签名、干净 Windows 环境、目标客户环境、实际安装/升级/卸载，以及恢复后关键功能需要按实际交付要求另行验收。发布证据必须记录实际机器和提交，不能复制历史收据或伪造 PASS。

`installer-candidate-manifest.json` 记录构建时状态；构建时尚未真实安装是正常事实。后续机器上的实际验收应单独保存带时间、源码和安装包身份的结果，不能回写或覆盖原始构建记录。