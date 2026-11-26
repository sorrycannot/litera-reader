# -*- coding: utf-8 -*-
"""
命令行抽取预览 —— 验收段落切分用

这是整个工具的地基。段落切错了，下面挂的中文就会整体错位，
而你英文读得慢，根本发现不了错位，会把上一段的意思当成这一段记住。

所以换新文献类型时，先跑这个看看切得准不准。

  python dev/extract_preview.py 论文.pdf

输出 out/<文件名>.preview.md，重点看：
  1. 段落有没有被切碎（一段拆成几条）或粘连（两段并成一条）
  2. 双栏论文的顺序是否连贯
  3. 页眉页脚、参考文献有没有残留
  4. 标着「可疑」的段落是否确实有问题
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.pdf_reader import parse_pdf, render_preview   # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="PDF 段落抽取预览")
    ap.add_argument("pdf")
    ap.add_argument("--out", default="out", help="输出目录，默认 out")
    args = ap.parse_args()

    pdf = Path(args.pdf)
    if not pdf.is_file():
        print(f"文件不存在：{pdf}")
        return 1

    struct = parse_pdf(pdf)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / f"{pdf.stem}.preview.md"
    md.write_text(render_preview(struct), encoding="utf-8")

    trusted = sum(1 for p in struct.paragraphs if p.trusted)
    print(f"页数 {struct.pages}｜版面 {'双栏' if struct.columns == 2 else '单栏'}｜正文 {struct.body_size}pt")
    print(f"段落 {len(struct.paragraphs)} 段｜可信 {trusted}｜可疑 {len(struct.paragraphs) - trusted}")
    print(f"总字符 {struct.total_chars:,}｜预估翻译成本约 ¥{struct.total_chars / 3.5 / 1e6 * 0.15 * 7.1 + struct.total_chars / 1e6 * 0.6 * 7.1:.3f}")
    print(f"\n预览：{md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
