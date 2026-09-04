# LTO LTFS Manager

面向现场磁带备份流程的 Qt6 图形界面，基于 LTFS，不使用 tar 打包。

主要面向 AlmaLinux 8 x86_64 的 LTO/LTFS 工作流。欢迎通过
[Issues](https://github.com/super-administrator/lto-tape-gui/issues) 反馈问题，
或提交 Pull Request 改进项目。

## 获取项目

```bash
git clone https://github.com/super-administrator/lto-tape-gui.git
cd lto-tape-gui
```

## 已实现功能

- 磁带机状态区：
  - 扫描设备ID：`ltfs -o device_list`
  - 挂载：`ltfs -o devname=<设备ID> <挂载目录>`
  - 卸载挂载：`umount <挂载目录>`，结束受管理的 LTFS 挂载进程
  - 弹出磁带：`mt -f /dev/st0 offline`（带倒带弹出）
  - 卷带保养：`mt -f /dev/nst0 retension`（长期存放后维护磁带张力）
  - 格式化：`mkltfs -d <设备ID>`（支持强制 `-f`）
  - 健康度检查：读取设备信息、错误计数及累计运行小时数，显示中文解读
- 文件备份区：
  - 可一次选择多个源文件或文件夹，按列表顺序逐项备份
  - 任一项目失败时停止队列，后续项目不会继续写入
  - 选择目标目录
  - 二次确认弹窗（源目录、目标目录、源大小、目标剩余空间）
  - 目标目录强校验：必须是已挂载 LTFS，否则拒绝备份（防止误写本地盘）
  - 两种复制方式：
    - `rsync -avh --info=progress2`（可选原地写入及跳过已存在文件）
    - `ltfs_ordered_copy -r`（不保留扩展属性，兼容当前 LTFS 挂载）
- 状态显示区：
  - 实时进度条
  - `ltfs_ordered_copy` 默认使用忙碌进度条（避免错误百分比）
  - 实时日志（命令与反馈）
- 右侧状态区：
  - 磁带机在线/挂载状态图标
  - 磁带使用百分比（饼图）
  - POH/MMH 运行小时数，持久化至 `runtime_hours.txt`
- 底部状态栏：
  - 上传/下载/磁带状态占位
- 任务控制：
  - 新增“取消当前任务”
  - 所有外部命令带超时控制（普通命令与备份命令可分别配置）
  - 同一时间仅允许一个磁带任务运行；备份期间不能卸载、挂载或格式化

## 项目结构

```text
TAPE GUI/
├── README.md                   # 使用与维护说明
├── LICENSE                     # MIT 开源许可证
├── requirements.txt            # 生产依赖版本
├── config/default.json         # 设备及备份默认配置
├── assets/lto-tape-gui.svg      # 应用图标
├── src/tape_gui/
│   ├── __init__.py
│   ├── main.py                 # Qt6 主界面
│   ├── commands.py             # LTFS/rsync 命令层
│   ├── config.py               # 配置读取
│   └── runtime_state.py        # 运行小时数持久化
├── tests/                      # 命令、运行状态及界面测试
└── offline/
    └── almalinux8-x86_64-python3.11/
        ├── INSTALL.md          # 内网安装步骤
        ├── SHA256SUMS          # 离线文件校验清单
        ├── source/             # Python 3.11.16 源码包
        └── wheels/             # PySide6 6.8.3 及配套依赖
```

本地 `.venv/` 为开发环境，不属于部署文件。离线依赖仅保留当前基线的一份展开目录；
需要传输时可临时打包，不在项目中长期保留重复压缩副本。
`runtime_hours.txt` 是运行时生成的设备计数状态，不应当作为历史缓存删除。

## 依赖安装（AlmaLinux）

先安装 Python 3.11。AlmaLinux 8 的系统 Python 3.6 不适用于本项目；
可按下文离线安装说明构建独立的 Python 3.11。以下命令中的 `python3.11`
也可替换为 `/opt/lto-python311/bin/python3.11`。

```bash
sudo dnf install -y rsync
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

LTFS、磁带驱动和设备工具需另行安装：挂载和格式化使用 `ltfs`、`mkltfs`，
顺序备份使用 `ltfs_ordered_copy`，弹出和卷带使用 `mt`，健康度检查使用 IBM `itdt`。
这些工具不包含在 Python 依赖中；运行用户需具备设备访问和挂载权限。

## 内网离线部署（AlmaLinux 8 x86_64）

生产基线为 Python 3.11.16 与 PySide6 6.8.3。离线介质位于
`offline/almalinux8-x86_64-python3.11/`，包含 Python 源码与所有 PySide6 wheel。
请先按该目录中的 `INSTALL.md` 安装 Python，再从本地 wheel 目录安装依赖；不要使用
开发机 macOS 的 `.venv`。

GitHub 仓库仅包含离线安装说明与校验清单，Python 源码包和 wheel 文件需另行传输到
上述目录。克隆仓库后，先补齐离线介质，再按 `INSTALL.md` 校验和安装。

## 启动

```bash
# 在项目根目录执行（先按安装说明创建 .venv）
PYTHONPATH=src .venv/bin/python -m tape_gui.main
```

## 配置

统一编辑 `config/default.json`，设备 ID、磁带设备、诊断设备和挂载目录应与现场对应。
默认设备 ID 留空，首次使用时通过“扫描设备ID”获取自己的设备 ID；
可将确认后的 ID 写入配置供下次启动使用。设备路径同样需要核对。
默认使用 rsync，启用原地写入及增量备份；“增量备份”会跳过已存在的文件，
不会更新目标中已有的同名文件。启用 LTFS 顺序优化时，这两个 rsync 选项会关闭。

## 本地验证

在项目根目录执行已有测试（包含无显示窗口的 Qt 界面检查）：

```bash
PYTHONPATH=src .venv/bin/python -B -m unittest discover -s tests -v
```

这些测试不代替现场磁带机和介质验证。

## 说明

- “卸载挂载”执行文件系统卸载；“弹出磁带”单独执行倒带弹出。
- `release_mode` 支持：`auto` / `with_mount_point` / `without_mount_point`，用于格式化前内容检查结束后的设备释放，兼容不同 LTFS 版本。
- `minimum_free_bytes` 为备份保留的安全空间，默认 10 GiB；容量或源目录大小无法读取时会拒绝备份。
- 进度条对 `rsync --info=progress2` 解析较稳定；`ltfs_ordered_copy` 因输出格式差异使用忙碌条更稳妥。
- 在线状态以设备 ID 和挂载状态为主；详细设备状态通过“健康度检查”读取。
- POH 只在同一次 Linux 启动中按运行时间估算；MMH 以健康度检查的实际读数校准。

## 开源许可

本项目采用 [MIT License](LICENSE)。欢迎使用、修改和分发，分发时请保留版权及许可证声明。
PySide6/Qt、Python、IBM LTFS 和其他第三方组件分别遵循各自的许可证。
