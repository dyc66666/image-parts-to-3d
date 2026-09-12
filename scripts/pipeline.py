#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline.py — 图片 → 部件去背景 → 混元 3D → GLB，输出到单一工作文件夹

完整流水线：
  1. 读取图片，自动去背景（纯色底用颜色法，复杂背景用 macOS Vision）
  2. 按连通域分割成独立部件，逐个居中合成到纯白正方形画布
  3. 每个部件提交混元图生 3D（自动遵守并发上限 2）
  4. 下载 GLB 与预览图到工作文件夹
  5. 删除所有中间文件，只保留成品

用法：
    python3 pipeline.py --input ~/Desktop/photo.jpg --work-dir ./assets
    python3 pipeline.py --input a.png --work-dir ./assets --prompt "木质长凳" --no-split
"""

import argparse
import json
import os
import shutil
import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hunyuan3d
import prepare_parts as pp
import unity_export

MAX_CONCURRENT = 2  # 混元 3D 服务并发上限


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def prepare(src, prepared_dir, args):
    """去背景 + 部件分割，返回部件信息列表。"""
    os.makedirs(os.path.join(prepared_dir, "parts"), exist_ok=True)
    rgb_img, alpha_img, method, bg_used, uniform = pp.prepare_image(
        src, method=args.method, threshold=args.threshold)
    parts, masks = pp.prepare_segments(alpha_img, min_area=args.min_area,
                                      max_parts=args.max_parts, no_split=args.no_split)

    bg_color = tuple(int(v) for v in args.bg.split(","))
    cutout_dir = os.path.join(prepared_dir, "cutouts")
    os.makedirs(cutout_dir, exist_ok=True)
    for i, part in enumerate(parts):
        name = f"part_{i:02d}.png"
        path = os.path.join(prepared_dir, "parts", name)
        # 透明去背图：与喂给 3D 的白底图同构图，用作工具栏图标
        cutout_path = os.path.join(cutout_dir, name)
        pp.render_part(rgb_img, masks[i], part["bbox"], path,
                       size=args.size, padding=args.padding, bg_color=bg_color,
                       alpha_out_path=cutout_path)
        part["file"] = path
        part["cutout"] = cutout_path

    if bg_used is not None:
        bg_used = [int(round(v)) for v in bg_used]

    log(f"[INFO] 去背方法={method} 背景均匀度={uniform:.1f} 部件数={len(parts)}")
    return parts, method, bg_used, uniform


def generate(parts, work_dir, name, args, token):
    """按并发上限提交混元任务并下载结果。"""
    pending = list(enumerate(parts))
    running = {}
    results = []
    failed = []

    start = time.time()
    while pending or running:
        if time.time() - start > args.max_wait:
            log("[ERROR] 超出总等待时间，终止")
            for job, (idx, part) in running.items():
                failed.append({"part": idx, "job_id": job, "error": "timeout; remote job may still be running"})
            for idx, part in pending:
                failed.append({"part": idx, "error": "not submitted: timeout"})
            break

        while pending and len(running) < MAX_CONCURRENT:
            idx, part = pending.pop(0)
            try:
                job = hunyuan3d.submit(
                    token, image=part["file"], prompt=args.prompt,
                    model=args.model, generate_type=args.generate_type,
                    face_count=args.face_count, result_format=args.result_format,
                    enable_pbr=args.enable_pbr,
                )
                running[job] = (idx, part)
                log(f"[INFO] part_{idx:02d} 已提交 job={job}")
            except Exception as e:
                msg = str(e)
                log(f"[ERROR] part_{idx:02d} 提交失败：{msg}")
                failed.append({"part": idx, "error": msg})
                if "limit exceeded" in msg:
                    log("[ERROR] 已触及混元服务配额上限，停止提交后续部件")
                    failed.extend({"part": i, "error": "not submitted: " + msg} for i, _ in pending)
                    pending.clear()
                    break

        if not running:
            break

        time.sleep(args.poll_interval)
        for job in list(running):
            idx, part = running[job]
            try:
                r = hunyuan3d.query(job, token)
            except Exception as e:
                log(f"[WARN] part_{idx:02d} 查询异常：{e}")
                continue
            st = r.get("Status")
            if st == "DONE":
                suffix = "" if len(parts) == 1 else f"_part{idx:02d}"
                prefix = os.path.join(work_dir, f"{name}{suffix}")
                try:
                    # Download into an isolated directory first, so a stale model from
                    # an earlier run can never masquerade as this job's output.
                    with tempfile.TemporaryDirectory(prefix="_download_", dir=work_dir) as stage:
                        downloaded = hunyuan3d.download(r, os.path.join(stage, os.path.basename(prefix)))
                        if not downloaded:
                            raise RuntimeError("DONE response contains no model files")
                        if args.result_format is None and not any(f.lower().endswith(".glb") for f in downloaded):
                            raise RuntimeError("DONE response contains no GLB model")
                        files = []
                        for temporary in downloaded:
                            final = os.path.join(work_dir, os.path.basename(temporary))
                            os.replace(temporary, final)
                            files.append(final)
                except Exception as e:
                    failed.append({"part": idx, "job_id": job, "error": "download failed: " + str(e)})
                    log(f"[ERROR] part_{idx:02d} 下载失败：{e}")
                    del running[job]
                    continue
                results.append({
                    "part": idx,
                    "job_id": job,
                    "files": [os.path.basename(f) for f in files],
                    "credit": r.get("ResultCreditConsumed"),
                })
                log(f"[OK] part_{idx:02d} 完成 → {[os.path.basename(f) for f in files]}")
                del running[job]
            elif st == "FAIL":
                msg = r.get("ErrorMessage", "")
                log(f"[ERROR] part_{idx:02d} 生成失败：{msg}")
                failed.append({"part": idx, "job_id": job, "error": msg})
                del running[job]

    return results, failed


def main():
    p = argparse.ArgumentParser(description="图片 → 3D 部件流水线")
    p.add_argument("--input", required=True, help="输入图片")
    p.add_argument("--work-dir", default="./3d-assets", help="产物根目录")
    p.add_argument("--name", default=None, help="工作文件夹名（默认取图片名）")
    p.add_argument("--token", default=os.getenv("BUDDY_CLOUD_TOKEN", ""))
    p.add_argument("--token-stdin", action="store_true", help="从标准输入读取 WorkBuddy 临时凭证，优先于环境变量和 --token")

    # 图片处理
    p.add_argument("--method", default="auto", choices=["auto", "color", "vision"])
    p.add_argument("--threshold", type=int, default=42, help="背景色容差")
    p.add_argument("--min-area", type=float, default=0.015)
    p.add_argument("--max-parts", type=int, default=8)
    p.add_argument("--padding", type=float, default=0.10)
    p.add_argument("--size", type=int, default=1024)
    p.add_argument("--bg", default="255,255,255")
    p.add_argument("--no-split", action="store_true")

    # 3D 生成
    p.add_argument("--prompt", default=None, help="附加文本引导")
    p.add_argument("--model", default="3.0", choices=["3.0", "3.1"])
    p.add_argument("--generate-type", default="LowPoly",
                   choices=["Normal", "LowPoly", "Geometry", "Sketch"])
    p.add_argument("--face-count", type=int, default=30000)
    p.add_argument("--result-format", default=None, choices=["STL", "USDZ", "FBX"])
    p.add_argument("--enable-pbr", action="store_true")

    # 流程控制
    p.add_argument("--keep-parts", action="store_true", help="保留去背后的部件图")
    p.add_argument("--dry-run", action="store_true",
                   help="只做去背与部件分割，不提交 3D 生成（用于检查效果/省额度）")
    p.add_argument("--poll-interval", type=int, default=15)
    p.add_argument("--max-wait", type=int, default=1800)
    p.add_argument("--allow-degraded", action="store_true",
                   help="去背降级（复杂背景且无 Vision）时仍继续生成，不中止")
    unity_export.add_arguments(p)
    args = p.parse_args()

    src = os.path.abspath(args.input)
    if not os.path.isfile(src):
        p.error(f"图片不存在：{src}")
    name = args.name or os.path.splitext(os.path.basename(src))[0]
    if not name or name in (".", "..") or any(c in name for c in '/\\<>:"|?*') or name.endswith((".", " ")):
        p.error("--name 必须是安全的单层文件名，不能是目录路径")
    try:
        unity_export._basename(name, "--name")
        canonical_id = unity_export.sanitize_id(name)
        if args.item_id and args.item_id != canonical_id:
            raise ValueError("--item-id 必须与 --name 的规范化 ID 相同；请通过 --name 选择稳定名称")
    except ValueError as exc:
        p.error(str(exc))
    if not 128 <= args.size <= 5000 or args.max_parts < 1 or not 0 <= args.min_area <= 1:
        p.error("要求 128 <= size <= 5000、max-parts >= 1、0 <= min-area <= 1")
    if not 0 <= args.padding <= 2 or args.poll_interval < 0 or args.max_wait <= 0 or not 0 <= args.threshold <= 442:
        p.error("padding/等待时间/threshold 参数超出有效范围")
    try:
        color = [int(v) for v in args.bg.split(",")]
        if len(color) != 3 or any(v < 0 or v > 255 for v in color):
            raise ValueError()
    except ValueError:
        p.error("--bg 必须是 0~255 的 R,G,B 三个整数")
    if args.generate_type == "LowPoly":
        args.model = "3.0"  # Record the model actually sent to the service.
    if not args.dry_run:
        try:
            unity_export.preflight(args)
        except (ValueError, OSError, RuntimeError) as exc:
            p.error(str(exc))
    token = sys.stdin.read().strip() if args.token_stdin and not args.dry_run else args.token
    if not args.dry_run and not token:
        p.error("缺少 token：设置 BUDDY_CLOUD_TOKEN 或传 --token")

    work_dir = os.path.abspath(os.path.join(args.work_dir, name))
    os.makedirs(work_dir, exist_ok=True)
    # Never delete a previous run (the next --input may be one of its cutouts).
    prepared_dir = tempfile.mkdtemp(prefix="_prepared_", dir=work_dir)
    log(f"[INFO] 工作文件夹：{work_dir}")
    try:
        parts, method, bg_used, uniform = prepare(src, prepared_dir, args)
    except (ValueError, OSError) as exc:
        p.error(str(exc))

    if len(parts) != 1 and (args.item_id or args.display_name):
        p.error("多部件时不能共用 --item-id/--display-name；单个道具使用 --no-split，或分开导出各部件")

    degraded = bool(method == "color" and uniform >= 26)
    needs_ai_cutout = bool(degraded and not args.allow_degraded)
    if degraded:
        log("[WARN] 复杂背景已使用颜色去背，抠图可能不干净；建议先 AI 去背后重跑")

    manifest = {
        "schema_version": 2,
        "source": src,
        "work_dir": work_dir,
        "prepared_dir": prepared_dir,
        "unity_options": {key: value for key, value in vars(args).items()
                          if key.startswith("unity_") or key in
                          ("no_icon", "item_id", "display_name", "category", "price", "world_scale", "footprint")},
        "bg_removal": {"method": method, "background_color": bg_used,
                       "uniformity": round(float(uniform), 2),
                       "degraded": degraded, "needs_ai_cutout": needs_ai_cutout},
        "generation": {
            "model": args.model, "generate_type": args.generate_type,
            "face_count": args.face_count, "prompt": args.prompt,
            "result_format": args.result_format, "enable_pbr": args.enable_pbr,
        },
        "parts": [], "failed": [],
    }
    manifest_path = os.path.join(work_dir, "manifest.json")
    if needs_ai_cutout and not args.dry_run:
        manifest["status"] = "needs_ai_cutout"
        write_json(manifest_path, manifest)
        print(json.dumps({
            "error": "NEEDS_AI_CUTOUT", "uniformity": round(float(uniform), 2),
            "prepared_parts": os.path.join(prepared_dir, "parts"),
            "hint": "先用 AI 去背，保持原道具形状与配色，再以透明 PNG 重跑；或明确使用 --allow-degraded",
        }, ensure_ascii=False, indent=2))
        return 2

    if args.dry_run:
        log("[INFO] --dry-run：跳过生成及 Unity 写入，保留图片供检查")
        results, failed = [], []
        args.keep_parts = True
        manifest["prepared_parts"] = parts
    else:
        # Durable copies exist before downloading/copying/cleanup, including after
        # a service failure. They are the exact earlier cutouts, never GLB renders.
        if not args.no_icon:
            for idx, part in enumerate(parts):
                suffix = "" if len(parts) == 1 else f"_part{idx:02d}"
                icon = os.path.join(work_dir, f"{name}{suffix}.png")
                shutil.copy2(part["cutout"], icon)
                part["saved_icon"] = os.path.basename(icon)
        results, failed = generate(parts, work_dir, name, args, token)
        results.sort(key=lambda r: r["part"])
        for result in results:
            part = parts[result["part"]]
            if part.get("saved_icon"):
                result["icon"] = part["saved_icon"]
            result["bbox"] = part["bbox"]

    manifest["parts"], manifest["failed"] = results, failed
    manifest["status"] = "dry_run" if args.dry_run else "generated"
    write_json(manifest_path, manifest)  # Recovery point before touching Unity.
    unity = {"copied_to_unity": [], "copied_icons_to_unity": [],
             "copied_metadata_to_unity": [], "registration": "not_requested"}
    export_error = None
    if not args.dry_run and results:
        try:
            unity = unity_export.export_assets(work_dir, results, args)
        except (ValueError, OSError, RuntimeError) as exc:
            export_error = str(exc)
            unity["error"] = export_error
            unity["registration"] = "export_failed" if args.unity_copy else "not_requested"
            log(f"[ERROR] 资源打包/Unity 复制失败：{exc}；本地产物已保留，可用 unity_export.py --manifest 重试")

    # Clean only this run's intermediate directory after all requested outputs
    # are safely available. Never delete a model, local icon, or Unity .meta.
    if not args.keep_parts and not failed and not export_error:
        resolved = Path(prepared_dir).resolve()
        if resolved.parent != Path(work_dir).resolve() or not resolved.name.startswith("_prepared_"):
            raise RuntimeError("拒绝清理工作目录以外的路径")
        shutil.rmtree(resolved)
        manifest["prepared_dir"] = None
        log("[INFO] 已清理本次中间文件，保留模型、去背图与元数据")
    else:
        log(f"[INFO] 保留部件图于 {prepared_dir}")

    manifest["unity"] = unity
    if export_error:
        manifest["status"] = "export_failed"
    elif failed:
        manifest["status"] = "partial_failure" if results else "generation_failed"
    elif not args.dry_run:
        manifest["status"] = "complete"  # Pipeline completed; editor registration is separate.
    write_json(manifest_path, manifest)
    print(json.dumps({
        "work_dir": work_dir, "manifest": manifest_path,
        "bg_removal": method, "uniformity": round(float(uniform), 2),
        "degraded": degraded, "needs_ai_cutout": needs_ai_cutout,
        "part_count": len(parts), "succeeded": len(results), "failed": len(failed),
        "files": [f for r in results for f in r["files"]],
        "icons": [r["icon"] for r in results if r.get("icon")],
        **unity,
    }, ensure_ascii=False, indent=2))
    return 1 if failed or export_error else 0


def write_json(path, value):
    temporary = str(path) + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")
    os.replace(temporary, path)


if __name__ == "__main__":
    sys.exit(main())
