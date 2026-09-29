# Installation

本文档描述的是**本仓库源码**的行为（含行情ID本地索引等改动），与 PyPI 上的发布版并不完全一致，因此请安装本仓库，不要从 PyPI 拉取。

## 环境要求

```text
Python 3.6 及以上（pyproject.toml 声明），推荐 3.10 / 3.11 / 3.12
pip
Windows / Linux / macOS
```

不需要 Node.js。

```{warning}
**Windows 上的 `python` 命令可能是无效的。** 如果 `python` 执行后**没有任何输出**，说明命中的是 Microsoft Store 的占位程序，它不会真的运行 Python。改用 `py` 启动器，例如把下文的 `python` 换成 `py`。
```

## 安装本仓库

创建并激活虚拟环境（推荐）：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
```

在仓库根目录下以开发模式安装：

```powershell
python -m pip install -e .
```

只想装依赖、直接在工作区里运行：

```powershell
python -m pip install -r requirements.txt
```

运行依赖：

```text
requests  pandas  tqdm  retry  multitasking  jsonpath  rich  beautifulsoup4
```

装完后验证：

```python
import efinance as ef

ef.stock.get_realtime_quotes().head()
```

## 用 Docker 运行

仓库根目录有 `Dockerfile`，可以在当前目录下直接构建：

```bash
# 切换工作目录为该项目根目录
cd efinance
# 构建镜像（-t 指定镜像名称，. 表示用当前目录的 Dockerfile）
docker build -t efinance . --no-cache
# 以交互方式运行镜像（运行后自动删除容器，如不想删除可去掉 --rm）
docker run --rm -it efinance
```
