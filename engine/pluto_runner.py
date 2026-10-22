#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pluto_runner.py — Text-to-PLUTO 执行适配器。

把 t2pluto 生成的 spec 落成独立算例目录，调用已编译的 PLUTO 二进制，
回读 pluto.ini 校验参数注入，提取终态 vtk 指标并给出验收门。

原则：
  - 只复用注册模板的已编译二进制；v1 不改 definitions.h，不重新编译。
  - 每次运行写入独立 runs/<run_id>，绝不覆盖模板目录或既有算例。
  - 所有用户输入先经 t2pluto.normalize_spec 白名单校验，再进入固定命令。
"""
import argparse
import glob
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import t2pluto  # noqa: E402

DEFAULT_TIMEOUT_S = 600


# ---------------------------------------------------------------- 目录与落盘
def _wsl_path(path):
    """F:\\x\\y -> /mnt/f/x/y（仅用于 Windows 侧经 WSL 调 PLUTO）。"""
    p = Path(path).resolve()
    parts = list(p.parts)
    drive = parts[0].rstrip(":\\/").lower()
    return "/mnt/%s/%s" % (drive, "/".join(parts[1:]))


def _new_run_dir(case, fingerprint, workspace):
    root = Path(workspace) / "workspace" / case / "pluto" / "runs"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    base = "%s-%s" % (stamp, fingerprint)
    run_id, n = base, 1
    while (root / run_id).exists():
        n += 1
        run_id = "%s-%d" % (base, n)
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    return run_id, run_dir


def write_spec(spec, workspace):
    """把归一化 spec 存到 case 的 specs 目录，供审阅与复算。"""
    root = Path(workspace) / "workspace" / spec["case"] / "pluto" / "specs"
    root.mkdir(parents=True, exist_ok=True)
    path = root / ("%s.json" % t2pluto.spec_fingerprint(spec))
    if not path.exists():
        path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------- 执行
def _run_solver(run_dir, timeout_s):
    binary = Path(run_dir) / "pluto"
    log_path = Path(run_dir) / "run.log"
    t0 = time.time()
    if os.name == "nt":
        cmd = ["wsl.exe", "-d", os.environ.get("PLUTO_WSL_DISTRO", "Ubuntu"),
               "-e", "bash", "-lc",
               "cd %s && ./pluto -i pluto.ini" % _wsl_path(run_dir)]
    else:
        if not os.access(binary, os.X_OK):
            binary.chmod(binary.stat().st_mode | 0o111)
        cmd = ["./pluto", "-i", "pluto.ini"]
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        try:
            proc = subprocess.run(cmd, cwd=str(run_dir), stdout=log,
                                  stderr=subprocess.STDOUT, timeout=timeout_s)
            rc, timed_out = proc.returncode, False
        except subprocess.TimeoutExpired:
            rc, timed_out = -9, True
    elapsed = time.time() - t0
    tail = _tail(log_path, 3000)
    return {"rc": rc, "timed_out": timed_out, "elapsed_s": round(elapsed, 2),
            "command": " ".join(cmd), "log": str(log_path), "log_tail": tail}


def _tail(path, n):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        return text[-n:]
    except OSError:
        return ""


def _final_vtk(run_dir):
    files = glob.glob(str(Path(run_dir) / "data.*.vtk"))
    if not files:
        return None

    def idx(p):
        m = re.search(r"data\.(\d+)\.vtk$", p)
        return int(m.group(1)) if m else -1
    return Path(sorted(files, key=idx)[-1])


# ---------------------------------------------------------------- 指标
def _extract_metrics(vtk_path, mu_factor=1.0):
    extractor = Path(t2pluto.workspace_root()) / "scripts" / "pluto_vtk_extract.py"
    if not extractor.is_file():
        raise RuntimeError("指标提取器不存在：%s" % extractor)
    spec = importlib.util.spec_from_file_location("pluto_vtk_extract", str(extractor))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    npts, ncells, arrays = mod.parse_vtk(str(vtk_path))
    if "rho" not in arrays or "prs" not in arrays:
        raise RuntimeError("vtk 缺少 rho/prs 标量：%s" % sorted(arrays))
    rho, prs = arrays["rho"], arrays["prs"]
    max_rho = max(rho)
    max_porho = max((p / r for p, r in zip(prs, rho) if r > 0), default=0.0)
    if not (math.isfinite(max_rho) and max_rho > 0 and math.isfinite(max_porho) and max_porho > 0):
        raise RuntimeError("终态指标非有限正数：max_rho=%r max_p_over_rho=%r"
                           % (max_rho, max_porho))
    return {
        "npoints": npts,
        "ncells": ncells,
        "max_rho": max_rho,
        "max_p_over_rho": max_porho,
        "max_logp": math.log10(max_porho),
        "max_T_K": 120.3 * max_porho * mu_factor,
        "mu_factor": mu_factor,
        "max_T_calibrated": False,
        "vars": sorted(arrays),
    }


def _target_gate(metrics, target):
    if not target:
        return {"pass": None, "detail": "未提供 target"}
    checks = []
    for key, want in target.items():
        got = metrics.get(key)
        if got is None:
            checks.append({"name": key, "pass": False, "value": None, "target": want})
            continue
        if isinstance(want, list):
            lo, hi = float(want[0]), float(want[1])
            checks.append({"name": key, "pass": lo <= got <= hi, "value": got, "target": want})
        else:
            tol = abs(float(want)) * 0.05
            checks.append({"name": key, "pass": abs(got - want) <= tol,
                           "value": got, "target": want})
    return {"pass": all(c["pass"] for c in checks), "checks": checks}


# ---------------------------------------------------------------- 主流程
def run_case(spec, case=None, dry_run=True, timeout_s=DEFAULT_TIMEOUT_S,
             workspace=None, target=None):
    reg = t2pluto.load_registry()
    spec = t2pluto.normalize_spec(spec, registry=reg)
    if case:
        if not t2pluto.CASE_PATTERN.fullmatch(case):
            raise t2pluto.SpecError("case 名非法")
        spec["case"] = case
    if target is not None:
        spec["target"] = target
        spec = t2pluto.normalize_spec(spec, registry=reg)

    ws = Path(workspace or t2pluto.workspace_root())
    tpl_ini = t2pluto.template_ini(spec["template"], reg)
    binary = t2pluto.template_binary(spec["template"], reg)
    if not tpl_ini.is_file():
        raise RuntimeError("模板 pluto.ini 不存在：%s" % tpl_ini)
    if not binary.is_file():
        raise RuntimeError("模板 PLUTO 二进制不存在：%s" % binary)

    rendered = t2pluto.render_ini(spec, tpl_ini.read_text(encoding="utf-8", errors="replace"), reg)
    verify = t2pluto.verify_rendered_ini(rendered, spec, reg)
    fingerprint = t2pluto.spec_fingerprint(spec)

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "case": spec["case"],
            "template": spec["template"],
            "fingerprint": fingerprint,
            "spec": spec,
            "pluto_ini": rendered,
            "gates": {"dry_run": {"pass": True}, "param_injection": {"pass": True, "detail": verify}},
        }

    run_id, run_dir = _new_run_dir(spec["case"], fingerprint, ws)
    (run_dir / "pluto.ini").write_text(rendered, encoding="utf-8")
    (run_dir / "pluto_spec.json").write_text(
        json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copy2(binary, run_dir / "pluto")
    if os.name != "nt":
        (run_dir / "pluto").chmod(0o755)

    exec_info = _run_solver(run_dir, timeout_s)

    gates = {
        "solver_rc": {"pass": exec_info["rc"] == 0 and not exec_info["timed_out"],
                      "value": exec_info["rc"], "timed_out": exec_info["timed_out"]},
        "param_injection": {"pass": True, "detail": verify},
        "final_vtk": {"pass": False},
        "metrics_finite": {"pass": False},
        "target": {"pass": None},
    }
    metrics, vtk = None, _final_vtk(run_dir)
    if vtk is not None:
        gates["final_vtk"] = {"pass": True, "value": vtk.name}
        try:
            metrics = _extract_metrics(vtk, spec["output"]["mu_factor"])
            gates["metrics_finite"] = {"pass": True, "value": {
                "max_rho": metrics["max_rho"], "max_logp": metrics["max_logp"]}}
            gates["target"] = _target_gate(metrics, spec.get("target"))
        except Exception as exc:
            gates["metrics_finite"] = {"pass": False, "error": str(exc)[:500]}
    else:
        gates["final_vtk"] = {"pass": False, "error": "未生成 data.*.vtk"}

    overall = all(g["pass"] for g in gates.values() if g["pass"] is not None)
    result = {
        "ok": overall,
        "dry_run": False,
        "case": spec["case"],
        "run_id": run_id,
        "template": spec["template"],
        "fingerprint": fingerprint,
        "run_dir": str(run_dir),
        "elapsed_s": exec_info["elapsed_s"],
        "spec": spec,
        "metrics": metrics,
        "gates": gates,
        "artifacts": {
            "pluto_ini": str(run_dir / "pluto.ini"),
            "spec": str(run_dir / "pluto_spec.json"),
            "log": exec_info["log"],
            "vtk": str(vtk) if vtk else None,
            "result": str(run_dir / "result.json"),
        },
        "log_tail": exec_info["log_tail"][-1200:],
    }
    (run_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    latest = Path(ws) / "workspace" / spec["case"] / "pluto" / "latest.json"
    latest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def latest_result(case, workspace=None):
    ws = Path(workspace or t2pluto.workspace_root())
    path = ws / "workspace" / case / "pluto" / "latest.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- CLI
def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _summary(result):
    """桥/stdout 友好的精简结果，避免超过 8000 字符被截断。"""
    keep = ("ok", "dry_run", "case", "run_id", "template", "run_dir", "elapsed_s",
            "metrics", "gates", "artifacts", "error")
    out = {k: result[k] for k in keep if k in result}
    if result.get("dry_run") and "pluto_ini" in result:
        out["pluto_ini"] = result["pluto_ini"]
    return out


def main():
    ap = argparse.ArgumentParser(prog="pluto_runner", description="Text-to-PLUTO 执行适配器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("templates", help="列出注册模板与白名单参数")

    p = sub.add_parser("parse", help="自然语言 -> 归一化 spec")
    p.add_argument("--text", required=True)
    p.add_argument("--template", default=None)
    p.add_argument("--case", default="t2p-auto")
    p.add_argument("--out", default=None, help="spec 输出路径（JSON）")

    p = sub.add_parser("run", help="渲染/执行 PLUTO 算例")
    p.add_argument("--text", default=None)
    p.add_argument("--spec-file", default=None)
    p.add_argument("--case", required=True)
    p.add_argument("--template", default=None)
    p.add_argument("--confirm", action="store_true", help="无此标志只做 dry-run")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--target-file", default=None)
    p.add_argument("--target-json", default=None, help="内联目标 JSON（优先于文件）")
    p.add_argument("--out-root", default=None, help="workspace 根目录覆盖（测试用）")
    p.add_argument("--json", action="store_true", help="保留兼容，当前始终输出 JSON")
    p.add_argument("--summary", action="store_true", help="只输出精简结果（桥调用用）")

    p = sub.add_parser("extract", help="解析单个 vtk 终态")
    p.add_argument("--vtk", required=True)
    p.add_argument("--mu-factor", type=float, default=1.0)

    p = sub.add_parser("status", help="读最近一次结果")
    p.add_argument("--case", required=True)
    p.add_argument("--summary", action="store_true")

    args = ap.parse_args()

    try:
        if args.cmd == "templates":
            reg = t2pluto.load_registry()
            summary = {}
            for name, tpl in reg["templates"].items():
                summary[name] = {
                    "label": tpl["label"],
                    "binary": str(t2pluto.template_binary(name, reg)),
                    "parameters": {k: {"default": v["default"], "min": v["min"],
                                       "max": v["max"], "unit": v.get("unit")}
                                   for k, v in tpl["parameters"].items()},
                    "fixed": tpl.get("fixed_parameters", {}),
                }
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "parse":
            spec = t2pluto.parse_text(args.text, template=args.template, case=args.case)
            if args.out:
                Path(args.out).parent.mkdir(parents=True, exist_ok=True)
                Path(args.out).write_text(json.dumps(spec, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
            print(json.dumps(spec, ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "run":
            if bool(args.text) == bool(args.spec_file):
                raise t2pluto.SpecError("--text 与 --spec-file 必须二选一")
            if args.spec_file:
                spec = _load_json(args.spec_file)
                if args.case:
                    spec["case"] = args.case
            else:
                spec = t2pluto.parse_text(args.text, template=args.template, case=args.case)
            target = None
            if args.target_json:
                target = json.loads(args.target_json)
            elif args.target_file:
                target = _load_json(args.target_file)
            result = run_case(spec, case=args.case, dry_run=not args.confirm,
                              timeout_s=args.timeout, workspace=args.out_root, target=target)
            print(json.dumps(_summary(result) if args.summary else result,
                             ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 1

        if args.cmd == "extract":
            metrics = _extract_metrics(args.vtk, args.mu_factor)
            print(json.dumps(metrics, ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "status":
            result = latest_result(args.case)
            if result is None:
                print(json.dumps({"ok": False, "error": "尚未运行"}, ensure_ascii=False, indent=2))
                return 1
            print(json.dumps(_summary(result) if args.summary else result,
                             ensure_ascii=False, indent=2))
            return 0 if result.get("ok") else 1

    except (t2pluto.SpecError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:2000]}, ensure_ascii=False, indent=2))
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
