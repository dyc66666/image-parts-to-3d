#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hunyuan3d.py — 混元 3D 客户端（图生 3D / 图文生 3D）

复用 CodeBuddy 内置的多模态客户端完成请求签名，支持本地图片 base64 直传。

用法：
    python3 hunyuan3d.py submit --image part.png [--prompt "..."] [--model 3.0] \
        [--generate-type LowPoly] [--face-count 30000]
    python3 hunyuan3d.py query <job_id> --out out/hero
"""

import argparse
import base64
import glob
import importlib.util
import json
import os
import re
import shutil
import sys
import urllib.request

DEFAULT_BUDDY_SCRIPT = (
    "/Applications/CodeBuddy CN.app/Contents/Resources/app/extensions/genie/"
    "out/extension/builtin/buddy-multimodal-generation/scripts/buddy-cloud.py"
)

_bc = None


def _candidate_buddy_scripts():
    """候选的内置多模态客户端路径，按优先级排列（WorkBuddy / CodeBuddy 多平台）。"""
    cands = []
    env = os.environ.get("BUDDY_CLOUD_SCRIPT")
    if env:
        cands.append(env)
    home = os.path.expanduser("~")
    # WorkBuddy 插件缓存（Windows / macOS / Linux）
    cands.extend(sorted(glob.glob(os.path.join(
        home, ".workbuddy", "plugins", "cache", "workbuddy-builtin",
        "skill-buddy-multimodal-generation", "*", "scripts", "buddy-cloud.py"))))
    # CodeBuddy 内置目录
    cands.append(DEFAULT_BUDDY_SCRIPT)
    cands.extend(sorted(glob.glob(os.path.join(
        "/Applications", "CodeBuddy CN.app", "Contents", "Resources", "app",
        "extensions", "genie", "out", "extension", "builtin",
        "buddy-multimodal-generation", "scripts", "buddy-cloud.py"))))
    return cands


def _load_buddy():
    """加载内置的多模态客户端（负责签名与请求封装）。"""
    global _bc
    if _bc is not None:
        return _bc
    tried = []
    for path in _candidate_buddy_scripts():
        if path and os.path.exists(path):
            spec = importlib.util.spec_from_file_location("buddy_cloud", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _bc = mod
            return _bc
        if path:
            tried.append(path)
    sys.exit("[ERROR] 未找到内置多模态客户端，已尝试：\n  " + "\n  ".join(tried) +
             "\n可通过 BUDDY_CLOUD_SCRIPT 环境变量显式指定 buddy-cloud.py 路径。")


def _api(action, body, token):
    bc = _load_buddy()
    cfg = bc._PROVIDER_MAP["3d"]
    return bc._call_api(bc._DEFAULT_ENDPOINT, cfg["provider"], cfg["service"],
                        cfg["version"], action, body, token)


def build_body(image=None, image_url=None, prompt=None, model="3.0",
               generate_type=None, polygon_type=None, face_count=None,
               result_format=None, enable_pbr=False):
    body = {"Model": model}
    if image:
        with open(image, "rb") as f:
            raw = f.read()
        if 4 * ((len(raw) + 2) // 3) > 6 * 1024 * 1024:
            raise ValueError("图片 base64 后超过 6MB，请压缩后再试")
        body["ImageBase64"] = base64.b64encode(raw).decode()
    elif image_url:
        body["ImageUrl"] = image_url
    elif not prompt:
        raise ValueError("需要 image / image_url / prompt 之一")

    if prompt:
        body["Prompt"] = prompt
    if generate_type:
        # LowPoly 仅 3.0 支持
        if generate_type == "LowPoly" and model != "3.0":
            model = "3.0"
            body["Model"] = model
        body["GenerateType"] = generate_type
    if polygon_type:
        body["PolygonType"] = polygon_type
    if face_count:
        body["FaceCount"] = int(face_count)
    if result_format:
        body["ResultFormat"] = result_format
    if enable_pbr:
        body["EnablePBR"] = True
    return body


def submit(token, **kwargs):
    """提交生成任务，返回 job_id。"""
    body = build_body(**kwargs)
    bc = _load_buddy()
    cfg = bc._PROVIDER_MAP["3d"]
    r = _api(cfg["submit_action"], body, token)
    job_id = r.get("JobId")
    if not job_id:
        raise RuntimeError(f"提交失败：{json.dumps(r, ensure_ascii=False)}")
    return job_id


def query(job_id, token):
    """查询任务状态，返回原始结果 dict。"""
    bc = _load_buddy()
    cfg = bc._PROVIDER_MAP["3d"]
    return _api(cfg["query_action"], {"JobId": job_id}, token)


def download(result, out_prefix):
    """下载结果文件，返回保存路径列表。"""
    saved = []
    seen_preview = False
    for f in result.get("ResultFile3Ds", []):
        ext = str(f["Type"]).lower()
        if not re.fullmatch(r"[a-z0-9]{1,10}", ext):
            raise ValueError(f"非法模型文件类型：{ext}")
        path = f"{out_prefix}.{ext}"
        _download_file(f["Url"], path)
        saved.append(path)
        if f.get("PreviewImageUrl") and not seen_preview:
            pv = f"{out_prefix}_preview.png"
            try:
                _download_file(f["PreviewImageUrl"], pv)
                saved.append(pv)
            except Exception as exc:
                print(f"[WARN] 可选预览图下载失败，模型仍可使用：{exc}", file=sys.stderr)
            seen_preview = True
    return saved


def _download_file(url, path):
    temporary = path + ".download"
    try:
        with urllib.request.urlopen(url, timeout=120) as response, open(temporary, "wb") as stream:
            shutil.copyfileobj(response, stream)
        if os.path.getsize(temporary) == 0:
            raise RuntimeError("下载文件为空")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def main():
    p = argparse.ArgumentParser(description="混元 3D 客户端")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("submit", help="提交生成任务")
    ps.add_argument("--image")
    ps.add_argument("--image-url")
    ps.add_argument("--prompt")
    ps.add_argument("--model", default="3.0", choices=["3.0", "3.1"])
    ps.add_argument("--generate-type", choices=["Normal", "LowPoly", "Geometry", "Sketch"])
    ps.add_argument("--polygon-type", choices=["triangle", "quadrilateral"])
    ps.add_argument("--face-count", type=int)
    ps.add_argument("--result-format", choices=["STL", "USDZ", "FBX"])
    ps.add_argument("--enable-pbr", action="store_true")
    ps.add_argument("--token", default=os.getenv("BUDDY_CLOUD_TOKEN", ""))
    ps.add_argument("--token-stdin", action="store_true", help="Read a temporary credential from stdin")

    pq = sub.add_parser("query", help="查询并下载")
    pq.add_argument("job_id")
    pq.add_argument("--out", default="model")
    pq.add_argument("--token", default=os.getenv("BUDDY_CLOUD_TOKEN", ""))
    pq.add_argument("--token-stdin", action="store_true", help="Read a temporary credential from stdin")

    args = p.parse_args()
    token = sys.stdin.read().strip() if args.token_stdin else args.token
    if not token:
        sys.exit("[ERROR] 缺少 token：设置 BUDDY_CLOUD_TOKEN 或传 --token")

    if args.cmd == "submit":
        job = submit(token, image=args.image, image_url=args.image_url,
                     prompt=args.prompt, model=args.model,
                     generate_type=args.generate_type,
                     polygon_type=args.polygon_type,
                     face_count=args.face_count,
                     result_format=args.result_format,
                     enable_pbr=args.enable_pbr)
        print(json.dumps({"job_id": job}, ensure_ascii=False))
    else:
        r = query(args.job_id, token)
        if r.get("Status") == "DONE":
            files = download(r, args.out)
            print(json.dumps({"status": "DONE", "files": files}, ensure_ascii=False))
        else:
            print(json.dumps({"status": r.get("Status"),
                              "error": r.get("ErrorMessage", "")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
