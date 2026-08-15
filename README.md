# Abaqus 2022 安全 Python 自动化与钢管桩 CEL Phase 0 复现

[![portable-contracts](https://github.com/qofotel201-coder/0816Abaqus/actions/workflows/ci.yml/badge.svg)](https://github.com/qofotel201-coder/0816Abaqus/actions/workflows/ci.yml)

本仓库把外部 CPython 与 Abaqus/CAE 2022 连接、CAE 隔离审计、版本化工作区、受限模型写入、ODB 限界读取和阻力比较代码整理成可在另一台 Windows 电脑复现的源码包。

当前版本的准确状态是：**Phase 0 自动化框架可复现，但不具备开算许可。** Data Check、用户子程序编译、Explicit 作业提交、真实 ODB 正向提取和四工况长算仍未完成。代码中的提交接口是硬门禁，不会因为修改配置而启动求解器。

## 架构与安全边界

    Windows CPython 3.10+
              |
         固定 JSON-RPC
              |
      Abaqus 2022 CAE noGUI
              |
      +-------+--------+---------+
      |                |         |
    只读审计        工作副本写入   ODB 只读
    Python 2.7       Python 2.7   odbAccess

核心规则：

- 冻结 CAE 永不被 Abaqus 直接打开；每次读取都使用请求专属副本，并核对源文件前后 SHA-256。
- 外部编排使用 CPython 3.10 以上；Abaqus worker 保持 Python 2.7 兼容语法。
- worker 只有固定方法表，不接受任意 Python、shell、eval、exec 或动态导入请求。
- 所有持久写操作采用 plan/commit、一次性确认令牌和拒绝覆盖策略。
- 本版本 solver_execution_enabled=false、allow_job_kill=false。

## 仓库包含与不包含的内容

包含：

- Python/Abaqus 桥接器和三个隔离 worker；
- 版本化工作区、安全清单与阻力比较实现；
- 通用 JSON Schema、机器配置模板和四工况定义；
- 43 项纯 Python/契约测试及 GitHub Actions；
- Windows 安装、Phase 0 复现、资源预检和准备度判断脚本。

公开仓库不包含：

- RAR、CAE、INP、ODB 及其他求解产物；
- 客户聊天截图、失败日志和本机运行报告；
- 第三方论文 PDF；
- 项目用户子程序原文。

私有输入的文件名、大小和 SHA-256 记录在 [外部数据清单](config/EXTERNAL_DATA_MANIFEST.json)。只有获得相应文件使用权的人员才能执行项目级复现。

## 新电脑要求

- Windows 10/11 x64，本地 NTFS 文件系统；
- 64 位 CPython 3.10 或更高版本，推荐稳定版 3.11；
- Abaqus/CAE 2022 及可用 CAE 许可证；
- 后续若要编译用户子程序，还需 Abaqus 2022 支持的 Visual Studio 与 Intel Fortran 工具链；
- Phase 0 建议至少 8 GiB 实时可用内存、140 GiB 可用磁盘；
- 不使用 UNC、网络盘、同步盘或 exFAT 作为运行工作区。

Abaqus 自带的 abaqus、odbAccess 和 caeModules 不是 pip 包，不要尝试通过 pip 安装。

## 私有输入验收

| 输入 | 必需 | 字节数 | SHA-256 |
|---|---:|---:|---|
| driven pile.cae | 是 | 1,262,108,672 | a32e7a72293fbf3e1b803d8b820965ce82e78f74a811729a613740c5c40029ac |
| Combined_VUAMP_VUSDFLD.for | 是 | 17,438 | 4889db06c2cf1f477cecaa7baf0ceabf6b5e8279f903fc080cf9d69edf3cfd43 |
| driven pile.rar | 否 | 541,029,107 | 79cd1a86152ebcacb7b53b891f82a8e25f9d8427137964fc2c30bb261a5e7adb |

用户子程序当前仅为静态审查基线，并未获准进入求解器。

## 一次性安装

以下命令在 PowerShell 中执行。示例路径必须改成目标电脑的真实路径。

1. 克隆仓库：

       git clone https://github.com/qofotel201-coder/0816Abaqus.git C:\GitHub\0816Abaqus
       cd C:\GitHub\0816Abaqus

2. 准备互相分离的目录，例如：

       C:\AbaqusPile\source
       C:\AbaqusPileRuntime
       C:\PrivateSubroutines

3. 把冻结 CAE 放入 source，把获授权的 FOR 放入私有子程序目录。不要先用 Abaqus 打开 CAE。

4. 运行无覆盖安装：

       powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1 -PythonExe "C:\Python311\python.exe" -RuntimeRoot "C:\AbaqusPileRuntime" -SourceCae "C:\AbaqusPile\source\driven pile.cae" -AbaqusCommand "C:\SIMULIA\Commands\abq2022.bat" -Subroutine "C:\PrivateSubroutines\Combined_VUAMP_VUSDFLD.for"

安装程序会：

- 创建被 Git 忽略的虚拟环境和 config/local 配置；
- 重新计算 CAE/FOR 哈希；
- 建立 bridge-runs、workspaces、cases、results、reports；
- 安装固定的验证依赖；
- 运行全部纯 Python 测试和公开树安全检查；
- 保持求解和进程终止关闭。

安装脚本拒绝覆盖已有虚拟环境或本机配置。需要重建时请先人工审计并移走旧目录。

## 复现 Phase 0

运行：

    powershell -ExecutionPolicy Bypass -File .\scripts\run-phase0.ps1 -SourceCae "C:\AbaqusPile\source\driven pile.cae" -RuntimeRoot "C:\AbaqusPileRuntime" -RequestedCpus 18

该命令依次执行：

1. 纯 Python 契约测试；
2. Abaqus 2022 / embedded Python 2.7 连接；
3. 强制副本上的模型摘要；
4. 深层模型指纹审计；
5. 工作区 worker 能力检查；
6. odbAccess 缺失文件负向契约；
7. CPU、内存、磁盘和 NTFS 资源预检；
8. 严格准备度判断。

成功的 Phase 0 报告状态为 PASS_WITH_NO_SOLVER。准备度报告在当前版本通常仍为 NOT_READY，因为 Explicit Token、Data Check、真实 ODB 和四工况被明确阻断。这是安全设计，不是安装失败。

## 创建版本化工作区

仅在 Phase 0 通过、磁盘空间充足时执行。该操作会保留约 1.26 GB CAE 副本：

    .\.venv\Scripts\python.exe .\scripts\create_workspace.py --automation-config .\config\local\automation.json --source-cae "C:\AbaqusPile\source\driven pile.cae" --label portable-phase0 --output "C:\AbaqusPileRuntime\reports\workspace-create.json"

脚本执行两阶段 plan/commit，返回 workspace UUID，并再次证明源 CAE 的大小、mtime 和 SHA-256 未改变。它不会打开 Abaqus 或提交求解。

## 开发与离线验证

Linux、WSL 或无 Abaqus 的电脑只能运行纯 Python 合同测试：

    python -m pip install -r requirements-dev.txt
    python -B -m unittest discover -s tests -v
    python -B scripts\verify_public_tree.py

GitHub Actions 执行同一组测试。真实 Abaqus 集成测试只能在具备 Abaqus 2022 和许可证的 Windows 主机上运行。

## 当前能力矩阵

| 能力 | 状态 |
|---|---|
| 外部 Python 到 Abaqus/CAE 2022 连接 | 已验证 |
| 强制 CAE 副本与源哈希保全 | 已验证 |
| 模型摘要和深层审计 | 已验证 |
| 版本化工作区创建 | 已验证 |
| 工作副本克隆、saveAs、FOR 路径绑定、writeInput | 原主机已验证，便携脚本仍需目标机复测 |
| FOR 静态检查 | 已验证 |
| FOR 编译、链接和 Data Check | 阻断 |
| 作业 submit/wait/受控 kill | 阻断或仅契约测试 |
| ODB inventory/extract | 负向与模拟契约已验证；无真实 ODB 正向证据 |
| B00/B10/B01/B11 物化与长算 | 阻断 |
| DR/DW/RW 和闭合算法 | 单元测试已验证；无真实数据 |

详见 [当前状态](docs/STATUS.md)、[复现指南](docs/REPRODUCIBILITY.md)、[安全说明](docs/SECURITY.md)和[执行计划](PLAN.md)。

## 常见问题

**为什么不能直接打开源 CAE？**

Abaqus openMdb 即使没有显式保存，也可能改变被打开文件的字节。桥接器因此始终先复制，再打开副本。

**为什么准备度仍是 NOT_READY？**

可连接 Abaqus 不等于可以安全计算。当前仍缺正式用户子程序编译/Data Check、Explicit Token、真实 ODB 和四工况验证。

**可以把 solver_execution_enabled 改成 true 吗？**

不能绕过。本版本作业管理器的 Data Check 和 submit 方法是硬门禁，修改 JSON 不会实现求解。

**为何不使用 Git LFS 上传 CAE？**

这些输入体积大，且可能包含客户或第三方权利。本仓库只公开代码和不可变哈希，二进制输入通过受控渠道提供。

## 权利与责任

公开发布范围见 [NOTICE.md](NOTICE.md)。本仓库没有附加开源许可证。使用者必须自行确认 Abaqus 许可、模型数据权利和工程适用性。
