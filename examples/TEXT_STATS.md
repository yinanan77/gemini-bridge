# 中文文本统计工具（`examples/text_stats.py`）

一个仅使用 Python 3 标准库实现的轻量中文文本统计模块与命令行工具，无第三方依赖、无网络访问。

## 统计口径

`count_text(text)` 返回包含以下 4 个整型字段的字典：

- `characters`：Python `str` 的 Unicode 码位总数（`len(text)`），不按字节或近似分词估算；`\r\n` 按 2 个码位计，组合 Emoji 按实际码位计。
- `non_whitespace`：满足 `not ch.isspace()` 的码位数量（ASCII 空格、制表符、换行符、全角空格 `U+3000` 等空白字符不计入）。
- `lines`：行数，严格定义为 `len(text.splitlines())`（空字符串为 `0`；支持 LF、CRLF 等常见换行符）。
- `han_characters`：仅统计基本汉字区（CJK 统一表意文字 `U+4E00..U+9FFF`）内的字符；中文标点、Emoji 以及扩展 A/B 区或兼容表意文字不计入。

特殊说明：
- 空字符串 `""` 返回 `{"characters": 0, "non_whitespace": 0, "lines": 0, "han_characters": 0}`。
- 传入非 `str` 类型时抛出 `TypeError`。

## Python API 用法

```python
from examples.text_stats import count_text

count_text("")
# {'characters': 0, 'non_whitespace': 0, 'lines': 0, 'han_characters': 0}

count_text("你好 Gemini!\n第二行")
# {'characters': 14, 'non_whitespace': 12, 'lines': 2, 'han_characters': 5}
```

## 命令行（CLI）用法

命令行接收单个参数：UTF-8 文本文件路径，或 `-`（从标准输入读取 UTF-8 字节并保留原始换行符），向标准输出打印单个 JSON 对象。

```bash
# 统计 UTF-8 文件
python3 examples/text_stats.py path/to/file.txt

# 从标准输入统计
printf '你好 Gemini!\n第二行' | python3 examples/text_stats.py -
```

输出示例：

```json
{"characters": 14, "non_whitespace": 12, "lines": 2, "han_characters": 5}
```

错误处理：
- 当参数无效、文件不存在/不可读或输入包含非法 UTF-8 字节时，向 `stderr` 输出简洁错误信息（不打印 Python traceback），并以非零退出码退出。

## 编译检查与单元测试

```bash
python3 -m py_compile examples/text_stats.py
python3 -m unittest discover -s examples -p test_text_stats.py
```
