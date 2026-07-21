# paper2epub

将 arXiv 论文转换为 EPUB3，支持数学公式、图表交叉引用、TikZ 图片提取和中英双语翻译。

## 用法

```bash
# 生成 EPUB
uv run paper2epub.py 2402.08954

# 生成中英双语 EPUB
DASHSCOPE_API_KEY=your-key uv run paper2epub.py 2402.08954 --translate

# 生成后通过邮件发送
EMAIL_FROM=you@gmail.com EMAIL_TO=recipient@example.com EMAIL_PASSWORD=app-password \
  uv run paper2epub.py 2402.08954 --email
```

输出文件为 `<arxiv-id>.epub`；翻译模式输出 `<arxiv-id>-zh.epub`。

## 依赖

- [uv](https://docs.astral.sh/uv/)：运行脚本并自动安装 Python 依赖
- [pandoc](https://pandoc.org/installing.html)：生成 EPUB3
- `curl`：下载 arXiv 源码和必要的 PDF

可选环境变量：

| 变量 | 用途 |
|------|------|
| `DASHSCOPE_API_KEY` | `--translate` 使用的阿里百炼 API 密钥 |
| `EMAIL_FROM` / `EMAIL_TO` / `EMAIL_PASSWORD` | `--email` 的邮件配置 |
| `SMTP_SSL_HOST` / `SMTP_SSL_PORT` | SMTP 地址和端口，默认 `smtp.gmail.com:465` |
| `SMTP_PROXY` | SOCKS5 代理，例如 `socks5://127.0.0.1:1080` |
| `https_proxy` | 下载 arXiv 文件时使用的代理 |

## 转换内容

脚本下载并整理 LaTeX 源码，将 PDF 图片转为 PNG；论文包含 TikZ 图时，还会从 arXiv PDF 中提取渲染后的图片。随后由 pandoc 生成带目录、MathML 公式和交叉引用的 EPUB3。

图、表和公式会沿用论文的编号范围：普通 article 使用连续编号，含 chapter 的文档使用 `Figure 1.1`、`Table 2.1`、`(2.1)` 等章节编号，附录使用 `Figure A.1` 等编号。TikZ 组合图的子图引用会保留为 `Figure 1.7a`、`Figure 1.7b` 等形式。

使用 `--translate` 时，英文和中文段落交替排列。

## 测试

```bash
uv run --with pytest --with 'pylatexenc>=2.10,<3' \
  python -m pytest test_paper2epub.py -q
```

生成真实 EPUB 后，建议抽样对照源 PDF 的开头、后续章节和附录，检查图片、caption、编号、交叉引用和邻近正文。

## 主要文件

| 文件 | 说明 |
|------|------|
| `paper2epub.py` | 主脚本 |
| `filter.lua` | pandoc Lua 过滤器 |
| `epub.css` | EPUB 样式 |
| `test_paper2epub.py` | 测试 |
| `AGENTS.md` | 开发约束和架构说明 |
