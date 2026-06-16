# paper2epub

将 arXiv 论文的 LaTeX 源码下载并转换为 EPUB 电子书，支持中文翻译（双语沉浸式阅读）。

## 用法

```bash
uv run paper2epub.py <arxiv-id>
```

```bash
# 示例
uv run paper2epub.py 2402.08954

# 带中文翻译（双语沉浸式格式）
DASHSCOPE_API_KEY=your-key uv run paper2epub.py 2402.08954 --translate

# 转换后通过邮件发送 EPUB
EMAIL_FROM=you@gmail.com EMAIL_TO=recipient@example.com EMAIL_PASSWORD=app-password \
  uv run paper2epub.py 2402.08954 --email
```

Python 依赖（`pypdfium2`、`openai`）通过 PEP 723 内联元数据声明，`uv run` 会自动在隔离环境中安装，无需手动 `pip install`。

脚本会从 arxiv.org 下载 LaTeX 源码压缩包，解压到 `paper/` 目录，找到主 `.tex` 文件，通过 pandoc 转换生成 `<arxiv-id>.epub`。

使用 `--translate` 时，脚本调用阿里百炼 Qwen3.6-Flash 模型将论文翻译为中文，生成双语 EPUB（`<arxiv-id>-zh.epub`），英文和中文段落交替排列。

## 系统配置

### uv

需要 [uv](https://docs.astral.sh/uv/) 来运行脚本（自动管理 Python 和依赖）：

```bash
# macOS
brew install uv

# 或通用安装
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### pandoc

需要 [pandoc](https://pandoc.org/installing.html) 用于 LaTeX 到 EPUB3 的转换。

macOS：

```bash
brew install pandoc
```

Ubuntu / Debian：

```bash
sudo apt install pandoc
```

Arch Linux：

```bash
sudo pacman -S pandoc
```

Windows：

```bash
winget install pandoc
```

或从 [pandoc releases](https://github.com/jgm/pandoc/releases) 下载安装包。

### curl

需要 `curl` 下载 arXiv 源码压缩包。大多数系统已预装。

macOS：

```bash
# 系统自带，无需安装
```

Ubuntu / Debian：

```bash
sudo apt install curl
```

### 环境变量

| 变量 | 说明 |
|------|------|
| `DASHSCOPE_API_KEY` | `--translate` 模式必需，阿里百炼 API 密钥 |
| `EMAIL_PASSWORD` | `--email` 模式必需，SMTP 密码（Gmail 需使用应用专用密码） |
| `EMAIL_FROM` | `--email` 模式必需，发件人邮箱地址 |
| `EMAIL_TO` | `--email` 模式必需，收件人邮箱地址 |
| `SMTP_SSL_HOST` | SMTP 服务器地址，默认 `smtp.gmail.com` |
| `SMTP_SSL_PORT` | SMTP SSL 端口，默认 `465` |
| `https_proxy` | 代理配置，脚本通过 `curl` 下载，支持标准代理环境变量 |

```bash
# 代理示例
export https_proxy=http://127.0.0.1:7890
uv run paper2epub.py 2402.08954
```

## 转换流程

1. 下载并解压 arXiv 源码压缩包
2. 查找主 `.tex` 文件（匹配 `\documentclass` 或 `\begin{document}`）
3. 简化 `\documentclass` 以兼容 pandoc
4. 提取论文标题和作者（支持宏展开）
5. 通过 pypdfium2 将 PDF 图片转换为 PNG
6. 将 `.tex` 文件中的 `.pdf` 图片引用改写为 `.png`
7. 解析 LaTeX 交叉引用（`\ref`、`\autoref`、`\cref`、`\eqref`）
8. 预处理 algorithm/algorithmic 环境为 pandoc 可处理的格式
9. （可选）通过 Qwen3.6-Flash 翻译为中文，采用两阶段策略：术语表提取 → 上下文感知段落翻译
10. 运行 pandoc 生成 EPUB3
11. （可选）通过 SMTP SSL 发送 EPUB 到指定邮箱

## 输出格式

- EPUB3 格式
- 数学公式使用 MathML 渲染
- 自动生成目录（`--toc`）
- 章节自动编号（`--number-sections`）
- 使用 `epub.css` 控制排版样式

## 文件说明

| 文件 | 说明 |
|------|------|
| `paper2epub.py` | 主脚本 |
| `epub.css` | EPUB 排版样式表 |
| `filter.lua` | pandoc Lua 过滤器（图表编号、交叉引用处理） |
| `paper/` | 临时工作目录，每次运行时重建 |
