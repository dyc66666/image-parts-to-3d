#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prepare_parts.py — 图片预处理：去背景 → 部件分割 → 白底合成

把一张原始图片处理成若干张可直接喂给混元图生 3D 的"干净部件图"：
背景剔除、主体居中、留白均匀、纯色底。

仅依赖 Pillow + numpy，无需下载任何模型。

用法：
    python3 prepare_parts.py --input photo.jpg --out-dir work/prepared
    python3 prepare_parts.py --input photo.jpg --out-dir out --method vision
    python3 prepare_parts.py --input photo.jpg --out-dir out --no-split
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import deque

import numpy as np
from PIL import Image, ImageFilter, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
VISION_HELPER = os.path.join(HERE, "vision_mask.swift")


# --------------------------------------------------------------------------
# 去背景
# --------------------------------------------------------------------------

def corner_colors(arr, k=24):
    """采样四角颜色，用于判断背景是否为纯色。"""
    h, w = arr.shape[:2]
    k = max(2, min(k, h // 4 or 1, w // 4 or 1))
    patches = [arr[:k, :k], arr[:k, -k:], arr[-k:, :k], arr[-k:, -k:]]
    return np.array([np.median(p.reshape(-1, 3), axis=0) for p in patches])


def bg_uniformity(cols):
    """四角颜色的最大两两距离，值越小背景越可能是纯色。"""
    d = 0.0
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            d = max(d, float(np.linalg.norm(cols[i] - cols[j])))
    return d


def downscale_factor(h, w, max_side=512):
    return max(1, int(np.ceil(max(h, w) / max_side)))


def label_components(mask, max_side=512):
    """8-连通域标记（在下采样图上执行以保证速度）。

    返回 (labels, components, scale)，components[i] 为该连通域的像素坐标列表。
    """
    h, w = mask.shape
    s = downscale_factor(h, w, max_side)
    dh, dw = max(1, h // s), max(1, w // s)
    small = np.asarray(
        Image.fromarray((mask * 255).astype(np.uint8)).resize((dw, dh), Image.NEAREST)
    ) > 127

    labels = -np.ones((dh, dw), dtype=np.int32)
    comps = []
    cur = 0
    for y in range(dh):
        row = small[y]
        for x in range(dw):
            if not row[x] or labels[y, x] >= 0:
                continue
            q = deque([(y, x)])
            labels[y, x] = cur
            pix = []
            while q:
                cy, cx = q.popleft()
                pix.append((cy, cx))
                for ny in (cy - 1, cy, cy + 1):
                    if ny < 0 or ny >= dh:
                        continue
                    for nx in (cx - 1, cx, cx + 1):
                        if nx < 0 or nx >= dw:
                            continue
                        if small[ny, nx] and labels[ny, nx] < 0:
                            labels[ny, nx] = cur
                            q.append((ny, nx))
            comps.append(pix)
            cur += 1
    return labels, comps, s


def upsample_labels(labels, h, w):
    """把下采样的 label 图最近邻放大回原尺寸。"""
    dh, dw = labels.shape
    ys = np.linspace(0, dh - 1, h).astype(np.int32)
    xs = np.linspace(0, dw - 1, w).astype(np.int32)
    return labels[ys][:, xs]


def remove_bg_color(arr, threshold=42, softness=1.3, morph=True, max_side=512):
    """基于背景色 + 边缘连通性的去背景。适用于纯色/近纯色背景。"""
    cols = corner_colors(arr)
    bg = np.median(cols, axis=0)
    diff = np.sqrt(((arr.astype(np.int16) - bg) ** 2).sum(axis=2))

    # 候选背景：与背景色接近的像素
    cand = diff < threshold
    if not cand.any():
        return np.ones(arr.shape[:2], dtype=np.uint8) * 255, bg

    labels, _, s = label_components(cand, max_side)
    dh, dw = labels.shape
    border = set()
    for v in list(labels[0, :]) + list(labels[-1, :]) + list(labels[:, 0]) + list(labels[:, -1]):
        if v >= 0:
            border.add(v)

    lab_up = upsample_labels(labels, arr.shape[0], arr.shape[1])
    bg_mask = np.isin(lab_up, list(border)) & cand

    alpha = (~bg_mask).astype(np.uint8) * 255
    a = Image.fromarray(alpha, mode="L")
    if softness > 0:
        a = a.filter(ImageFilter.GaussianBlur(softness))
    if morph:
        a = a.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
    return np.asarray(a), bg


def remove_bg_vision(img_path, arr, softness=1.0):
    """用 macOS Vision 前景抠图（macOS 12+），适合复杂背景照片。失败返回 None。"""
    if not (sys.platform == "darwin" and os.path.exists(VISION_HELPER)):
        return None
    try:
        # Feed Vision the already oriented pixels, without EXIF metadata. This
        # also avoids writing a shared temporary mask beside a read-only input.
        with tempfile.TemporaryDirectory(prefix="image-parts-vision-") as tmp:
            oriented_png = os.path.join(tmp, "input.png")
            out_png = os.path.join(tmp, "mask.png")
            Image.fromarray(arr).save(oriented_png, "PNG")
            subprocess.run(
                ["swift", VISION_HELPER, oriented_png, out_png],
                check=True, capture_output=True, timeout=180,
            )
            with Image.open(out_png) as source_mask:
                m = source_mask.convert("L")
            m = m.resize((arr.shape[1], arr.shape[0]), Image.LANCZOS)
            if softness > 0:
                m = m.filter(ImageFilter.GaussianBlur(softness))
            return np.asarray(m)
    except Exception:
        return None


def prepare_image(src, method="auto", threshold=42):
    """Return RGB, alpha, actual method, background color and uniformity.

    Existing PNG/WebP/palette transparency is authoritative: preserve it
    instead of interpreting arbitrary RGB values in transparent pixels as a
    background color. Opaque inputs retain the original auto/color/Vision flow.
    RGB and alpha share the same EXIF-corrected coordinates.
    """
    if method not in ("auto", "color", "vision"):
        raise ValueError("method must be auto, color or vision")
    with Image.open(src) as source:
        rgba = ImageOps.exif_transpose(source).convert("RGBA")
    rgb_img = rgba.convert("RGB")
    arr = np.asarray(rgb_img)
    uniform = bg_uniformity(corner_colors(arr))
    original_alpha = rgba.getchannel("A")
    if original_alpha.getextrema()[0] < 255:
        return rgb_img, original_alpha, "alpha", None, uniform

    if method == "auto":
        method = "color" if uniform < 26 else "vision"
    alpha, bg_used = None, None
    if method == "vision":
        alpha = remove_bg_vision(src, arr)
        if alpha is None:
            print("[WARN] Vision 抠图不可用，回退到颜色去背", file=sys.stderr)
            method = "color"
    if alpha is None:
        alpha, bg_used = remove_bg_color(arr, threshold=threshold)
        bg_used = [int(round(v)) for v in bg_used]
    return rgb_img, Image.fromarray(alpha.astype(np.uint8)), method, bg_used, uniform


# --------------------------------------------------------------------------
# 部件分割
# --------------------------------------------------------------------------

def split_parts(alpha, min_area=0.015, max_parts=8, max_side=512):
    """把前景 alpha 按连通域切成多个部件，返回部件 bbox 列表（按面积降序）。"""
    fg = alpha > 127
    if not fg.any():
        return []

    labels, comps, s = label_components(fg, max_side)
    h, w = alpha.shape
    total = h * w
    parts = []
    for pix in comps:
        if len(pix) < 3:
            continue
        ys = np.array([p[0] for p in pix]) * s
        xs = np.array([p[1] for p in pix]) * s
        # 映射回原尺寸时的边界收敛
        y0, y1 = int(ys.min()), min(h, int(ys.max() + s))
        x0, x1 = int(xs.min()), min(w, int(xs.max() + s))
        bw, bh = x1 - x0, y1 - y0
        if bw <= 1 or bh <= 1:
            continue
        area_ratio = (bw * bh) / total
        if area_ratio < min_area:
            continue
        parts.append({"bbox": (x0, y0, x1, y1), "area_ratio": round(area_ratio, 4)})

    parts.sort(key=lambda p: p["area_ratio"], reverse=True)
    return parts[:max_parts]


def _exact_labels(mask):
    """Label 8-connected foreground using row runs, without downsampling.

    Run-length union/find avoids a Python queue entry for every solid pixel.
    Keeping native resolution preserves thin props and non-divisible image edges.
    """
    h, w = mask.shape
    parents, runs, previous = [], [], []

    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for y in range(h):
        changes = np.flatnonzero(np.diff(np.r_[False, mask[y], False]))
        current = []
        start_previous = 0
        for x0, x1 in zip(changes[::2], changes[1::2]):
            index = len(parents)
            parents.append(index)
            while start_previous < len(previous) and previous[start_previous][1] < x0:
                start_previous += 1
            candidate = start_previous
            while candidate < len(previous) and previous[candidate][0] <= x1:
                parents[root(previous[candidate][2])] = root(index)
                candidate += 1
            current.append((int(x0), int(x1), index))
            runs.append((y, int(x0), int(x1), index))
        previous = current

    labels = np.full((h, w), -1, dtype=np.int32)
    ids = {}
    for y, x0, x1, index in runs:
        component_root = root(index)
        component_id = ids.setdefault(component_root, len(ids))
        labels[y, x0:x1] = component_id
    return labels, len(ids)


def _include_soft_edges(labels, alpha):
    """Assign translucent edge pixels to a single neighboring component.

    Opaque cores remain separate even if their antialiased fringes touch.
    A component never borrows another component's core or edge pixels.
    """
    unowned = (alpha > 0) & (labels < 0)
    if not unowned.any():
        return labels
    adjacent = np.asarray(
        Image.fromarray(unowned.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(3))
    ) > 0
    ys, xs = np.where((labels >= 0) & adjacent)
    pending = deque(zip(ys.tolist(), xs.tolist()))
    h, w = labels.shape
    while pending:
        y, x = pending.popleft()
        component = labels[y, x]
        for ny in range(max(0, y - 1), min(h, y + 2)):
            for nx in range(max(0, x - 1), min(w, x + 2)):
                if alpha[ny, nx] > 0 and labels[ny, nx] < 0:
                    labels[ny, nx] = component
                    pending.append((ny, nx))
    return labels


def prepare_segments(alpha_img, min_area=0.015, max_parts=8, no_split=False):
    """Return JSON-safe part metadata and a matching list of isolated L masks.

    Pass each returned mask to render_part with that part's bbox. Masks use
    source coordinates; no mask contains pixels owned by another component.
    --no-split keeps the complete prop, including detached handles/accessories.
    If the size filter rejects every component, retain the original whole prop.
    min_area retains the legacy bounding-box/image-area definition.
    """
    alpha = np.asarray(alpha_img, dtype=np.uint8)
    if alpha.ndim != 2:
        raise ValueError("alpha must be a two-dimensional mask")
    if not 0 <= min_area <= 1 or max_parts < 1:
        raise ValueError("min_area must be between 0 and 1; max_parts must be positive")
    full_mask = Image.fromarray(alpha)
    full_bbox = full_mask.getbbox()
    if full_bbox is None:
        raise ValueError("去背后无前景，请检查输入透明度、降低 --threshold 或换 --method")
    fallback = ([{"bbox": full_bbox, "area_ratio": 1.0}], [full_mask])
    if no_split:
        return fallback

    core = alpha > 127
    if not core.any():
        core = alpha > 0  # A wholly translucent prop is still valid foreground.
    labels, count = _exact_labels(core)
    labels = _include_soft_edges(labels, alpha)
    h, w = alpha.shape
    candidates = []
    for component in range(count):
        owned = labels == component
        if np.count_nonzero(owned) < 3:
            continue
        part_mask = Image.fromarray(np.where(owned, alpha, 0).astype(np.uint8))
        bbox = part_mask.getbbox()
        x0, y0, x1, y1 = bbox
        bw, bh = x1 - x0, y1 - y0
        ratio = bw * bh / (h * w)
        if bw <= 1 or bh <= 1 or ratio < min_area:
            continue
        candidates.append(({"bbox": bbox, "area_ratio": round(ratio, 4)}, part_mask))
    candidates.sort(key=lambda item: item[0]["area_ratio"], reverse=True)
    candidates = candidates[:max_parts]
    if not candidates:
        return fallback
    return [item[0] for item in candidates], [item[1] for item in candidates]


def render_part(rgb_img, alpha_img, bbox, out_path, size=1024,
                padding=0.10, bg_color=(255, 255, 255), alpha_out_path=None):
    """裁剪单个部件，居中合成到正方形纯色画布上。

    给出 alpha_out_path 时额外保存一张透明背景版本，用作工具栏图标。
    图标和 3D 输入来自同一次 RGBA 裁剪/居中/缩放。缩放时预乘 alpha，
    避免透明像素颜色泄漏；图标不烘焙白底，避免在工具栏产生白色边缘。
    """
    if size < 1 or padding < 0:
        raise ValueError("size must be positive and padding must be non-negative")
    x0, y0, x1, y1 = bbox
    crop_rgb = rgb_img.crop((x0, y0, x1, y1))
    crop_a = alpha_img.crop((x0, y0, x1, y1))
    w, h = crop_rgb.size
    side = max(8, int(round(max(w, h) * (1 + 2 * padding))))

    crop_rgba = crop_rgb.convert("RGBA")
    crop_rgba.putalpha(crop_a)
    cutout = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    cutout.paste(crop_rgba, ((side - w) // 2, (side - h) // 2))
    if side != size:
        cutout = cutout.convert("RGBa").resize((size, size), Image.LANCZOS).convert("RGBA")
    background = Image.new("RGBA", cutout.size, (*bg_color, 255))
    canvas = Image.alpha_composite(background, cutout).convert("RGB")
    canvas.save(out_path, "PNG")

    if alpha_out_path:
        cutout.save(alpha_out_path, "PNG")
    return canvas


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="去背景 + 部件分割，输出干净部件图")
    p.add_argument("--input", required=True, help="输入图片路径")
    p.add_argument("--out-dir", required=True, help="输出目录")
    p.add_argument("--method", default="auto", choices=["auto", "color", "vision"],
                   help="去背方法：auto=纯色背景用 color，否则尝试 vision")
    p.add_argument("--threshold", type=int, default=42,
                   help="背景色容差（RGB 欧氏距离），默认 42")
    p.add_argument("--min-area", type=float, default=0.015,
                   help="部件最小面积占比，默认 0.015")
    p.add_argument("--max-parts", type=int, default=8, help="最多保留部件数")
    p.add_argument("--padding", type=float, default=0.10, help="部件留白比例")
    p.add_argument("--size", type=int, default=1024, help="输出边长（正方形）")
    p.add_argument("--no-split", action="store_true", help="不分割，整图去背后输出")
    p.add_argument("--bg", default="255,255,255", help="合成底色，默认白色")
    p.add_argument("--keep-cutout", action="store_true", help="额外保存透明去背图")
    args = p.parse_args()

    src = os.path.abspath(args.input)
    if not os.path.exists(src):
        sys.exit(f"[ERROR] 图片不存在：{src}")

    out_dir = os.path.abspath(args.out_dir)
    parts_dir = os.path.join(out_dir, "parts")
    os.makedirs(parts_dir, exist_ok=True)

    rgb_img, alpha_img, method, bg_color_used, uniform = prepare_image(
        src, method=args.method, threshold=args.threshold)

    if args.keep_cutout:
        rgba = rgb_img.convert("RGBA")
        rgba.putalpha(alpha_img)
        rgba.save(os.path.join(out_dir, "cutout.png"))
        alpha_img.save(os.path.join(out_dir, "mask.png"))

    try:
        parts, part_masks = prepare_segments(
            alpha_img, min_area=args.min_area, max_parts=args.max_parts,
            no_split=args.no_split)
    except ValueError as exc:
        sys.exit(f"[ERROR] {exc}")

    bg_color = tuple(int(v) for v in args.bg.split(","))
    saved = []
    cutouts_dir = os.path.join(out_dir, "cutouts")
    if args.keep_cutout:
        os.makedirs(cutouts_dir, exist_ok=True)
    for i, (part, part_mask) in enumerate(zip(parts, part_masks)):
        name = f"part_{i:02d}.png" if len(parts) > 1 else "part_00.png"
        path = os.path.join(parts_dir, name)
        cutout_path = os.path.join(cutouts_dir, name) if args.keep_cutout else None
        render_part(rgb_img, part_mask, part["bbox"], path,
                    size=args.size, padding=args.padding, bg_color=bg_color,
                    alpha_out_path=cutout_path)
        part["file"] = os.path.join("parts", name)
        if cutout_path:
            part["cutout"] = os.path.join("cutouts", name)
        saved.append(path)

    meta = {
        "source": src,
        "method": method,
        "background_color": bg_color_used,
        "part_count": len(parts),
        "parts": parts,
    }
    with open(os.path.join(out_dir, "parts.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(json.dumps({
        "out_dir": out_dir,
        "method": method,
        "part_count": len(parts),
        "parts": [p["file"] for p in parts],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
