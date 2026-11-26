# -*- coding: utf-8 -*-
"""原型：验证「矢量图带 + 三线表规则线」判据在三篇真实 PDF 上的表现。不落地，验完删。"""
import sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pymupdf
from app.pdf_reader import _row_words, _split_cells, _NUMERIC_CELL, CAPTION_PAT, TABLE_MIN_CELLS, TABLE_MIN_ROWS

FILES = {
    "第三篇(AIVC)": r"E:/Edge浏览器下载位置/A Survey of AI Virtual Cells  Representation Learning Perturbation Prediction and Intervention Design.pdf",
    "第一篇(s00406)": r"E:/Edge浏览器下载位置/s00406-025-02028-6.pdf",
    "Cell": r"E:/Edge浏览器下载位置/1-s2.0-S0092867413003875-main.pdf",
}

VEC_MIN_DRAW = 60       # 一个矢量带至少这么多绘图元素
VEC_MIN_SPAN = 80       # 带高至少这么多 pt


def vector_bands(page, drawings):
    """把一页的矢量绘图按 y 分带，找出连续高密度带 = 纯矢量画的示意图。"""
    w, h = page.rect.width, page.rect.height
    pts = []
    for g in drawings:
        r = g["rect"]
        # 页眉/页脚的贯穿细线不是图元素；等宽的表格规则线也不算图元素
        if r.height < 2 and r.width > w * 0.6:
            continue
        if r.y0 < h * 0.07 or r.y1 > h * 0.96:
            # 只在没有其它证据时排除（页眉线），但整页大图会被裁一点点，可接受
            if r.height < 2 and r.width > w * 0.6:
                continue
        if r.width <= 0 or r.height <= 0:
            continue
        pts.append(r)
    if len(pts) < VEC_MIN_DRAW:
        return []

    step = 30.0
    bands = {}
    for r in pts:
        k = int(r.y0 // step)
        bands[k] = bands.get(k, 0) + 1
    # 连续带（允许空 1 格）分组
    keys = sorted(bands)
    groups, cur = [], [keys[0]]
    for k in keys[1:]:
        if k - cur[-1] <= 2:
            cur.append(k)
        else:
            groups.append(cur); cur = [k]
    groups.append(cur)

    out = []
    for g in groups:
        lo, hi = g[0] * step, (g[-1] + 1) * step
        sub = [r for r in pts if lo - step <= r.y0 < hi + step]
        if len(sub) < VEC_MIN_DRAW or hi - lo < VEC_MIN_SPAN:
            continue
        x0 = min(r.x0 for r in sub); x1 = max(r.x1 for r in sub)
        y0 = min(r.y0 for r in sub); y1 = max(r.y1 for r in sub)
        if x1 - x0 < w * 0.3:
            continue
        out.append([round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)])
    return out


def ruled_tables(page, drawings, vec_bands):
    """三线表：等宽等 x 的贯穿细横线 >=2 条，线间文字呈多列。"""
    w, h = page.rect.width, page.rect.height
    lines = []
    for g in drawings:
        r = g["rect"]
        if r.height < 2 and r.width > w * 0.6 and r.y0 > h * 0.07:
            lines.append((round(r.y0, 1), round(r.x0, 1), round(r.x1, 1)))
    # 按 (x0,x1) 分组（容差 3pt）
    groups = {}
    for y, x0, x1 in lines:
        key = next((k for k in groups if abs(k[0]-x0) < 3 and abs(k[1]-x1) < 3), None)
        if key is None:
            groups[(x0, x1)] = []
            key = (x0, x1)
        groups[key].append(y)
    words = page.get_text("words")
    rows = _row_words(words)
    rowinfo = []
    for row in rows:
        cells = _split_cells(row)
        ws = [c for cell in cells for c in cell]
        rowinfo.append({
            "y0": min(c[1] for c in ws), "y1": max(c[3] for c in ws),
            "n": len(cells),
            "dots": sum(1 for c in cells if all(w[4].strip(".") == "" for w in c)),
        })
    out = []
    for (x0, x1), ys in groups.items():
        if len(ys) < 2:
            continue
        ys = sorted(ys)
        y0, y1 = ys[0], ys[-1]
        if y1 - y0 < 20:
            continue
        band = [x0, y0, x1, y1]
        # 落在矢量图带里 → 是图里的线，不是表格
        if any(overlap_area(band, v) > 0.5 * area(band) for v in vec_bands):
            continue
        inside = [r for r in rowinfo if r["y0"] >= y0 - 2 and r["y1"] <= y1 + 2]
        if len(inside) < 2 or not any(r["n"] >= 2 for r in inside):
            continue
        out.append([round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)])
    return out


def area(b):
    return max(0, b[2]-b[0]) * max(0, b[3]-b[1])


def overlap_area(a, b):
    x = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    y = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return x * y


def merge_boxes(boxes):
    """重叠 >50% 合并取并集。"""
    out = []
    for b in sorted(boxes, key=lambda z: (z[1], z[0])):
        hit = None
        for r in out:
            if area(b) and overlap_area(b, r) > 0.5 * min(area(b), area(r)):
                hit = r; break
        if hit:
            hit[0]=min(hit[0],b[0]); hit[1]=min(hit[1],b[1])
            hit[2]=max(hit[2],b[2]); hit[3]=max(hit[3],b[3])
        else:
            out.append(list(b))
    return out


for name, path in FILES.items():
    d = pymupdf.open(path)
    print("=" * 70)
    print(name, "页数", d.page_count)
    for i in range(d.page_count):
        pg = d[i]
        dr = pg.get_drawings()
        vb = vector_bands(pg, dr)
        rt = ruled_tables(pg, dr, vb)
        ims = pg.get_image_info()
        if vb or rt or len(ims) > 3:
            print("  p%-3d 位图=%-4d 矢量带=%s 规则表=%s" % (
                i+1, len(ims),
                [[round(z) for z in b] for b in vb],
                [[round(z) for z in b] for b in rt]))
    d.close()
