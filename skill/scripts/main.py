#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jijiang-text-to-pluto — main.py（OpenClaw Skill Anatomy 编排层）

运行位置：优先 WSL Ubuntu（/mnt/f 可用时直接调引擎）；
否则经命令桥 127.0.0.1:8765 转发到 Ubuntu。引擎：
`/mnt/f/FusionAgentHub/pluto/pluto_runner.py`。

用法：
  python3 main.py templates
  python3 main.py spec  --text "..." --case demo [--template icf-rad1d-grey]
  python3 main.py preview --spec-file /mnt/f/.../specs/x.json --case demo
  python3 main.py run   --spec-file /mnt/f/.../specs/x.json --case demo --confirm
  python3 main.py status --case demo
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.request
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import validate as v  # noqa: E402

BRIDGE = "http://127.0.0.1:8765/run"
BRIDGE_HEALTH = "http://127.0.0.1:8765/health"
ENGINE = "/mnt/f/FusionAgentHub/pluto/pluto_runner.py"
WORKSPACE = "/mnt/f/FusionWorkspace"


class JijiangTextToPluto:
    """OpenClaw Skill：anatomy() 与 SKILL.md 双声明。"""

    def anatomy(self):
        return {
            "name": "jijiang-text-to-pluto",
            "interface": "RadHydro.PLUTO.Run",
            "risk_level": "high",
            "parallelizable": False,
            "avg_duration": 30,
            "inputs": {
                "text": {"type": "string", "required": False,
                         "desc": "自然语言算例描述（与 spec_file 二选一）"},
                "spec_file": {"type": "string", "required": False,
                              "desc": "已审阅 spec JSON 路径"},
                "case": {"type": "string", "required": True, "desc": "算例名"},
                "template": {"type": "string", "required": False,
                             "desc": "icf-multilayer-1d | icf-rad1d-grey"},
                "confirm": {"type": "bool", "required": True,
                            "desc": "执行确认；false 仅 dry-run"},
                "timeout": {"type": "int", "required": False, "desc": "求解超时秒"},
                "target": {"type": "object", "required": False,
                           "desc": "验收目标 {max_rho:[lo,hi], max_logp:[lo,hi]}"},
            },
            "outputs": {
                "ok": {"type": "bool"},
                "case": {"type": "string"},
                "run_id": {"type": "string"},
                "template": {"type": "string"},
                "spec_path": {"type": "string"},
                "pluto_ini": {"type": "string"},
                "metrics": {"type": "object"},
                "gates": {"type": "object"},
                "run_dir": {"type": "string"},
            },
            "dependencies": ["fusion-agent-bridge:8765",
                             "pluto:scan/runs/run_60", "pluto:T006_rad_1D",
                             "engine:pluto_runner.py",
                             "extractor:pluto_vtk_extract.py"],
            "state": {"requires": ["PLUTO templates", "compiled pluto binary"],
                      "creates": ["workspace/<case>/pluto/specs",
                                  "workspace/<case>/pluto/runs/<run_id>"],
                      "modifies": ["workspace/<case>/pluto/latest.json"]},
            "permissions": {
                "network": ["127.0.0.1:8765"],
                "filesystem": {"read": ["/mnt/f/PLUTO", "/mnt/f/FusionWorkspace/scripts"],
                               "write": ["/mnt/f/FusionWorkspace/workspace/<case>/pluto"]},
                "confirmation": "required",
            },
        }

    def execute(self, inputs):
        case = v.validate_case(inputs.get("case", ""))
        text = inputs.get("text")
        spec_file = inputs.get("spec_file")
        if bool(text) == bool(spec_file):
            raise v.ValidationError("text 与 spec_file 必须二选一")
        timeout = v.validate_timeout(inputs.get("timeout", 600))
        target = v.validate_target(inputs.get("target"))
        confirm = inputs.get("confirm") is True

        if text:
            spec = self.generate_spec(v.validate_text(text), case, inputs.get("template"))
            if not spec.get("ok"):
                return spec
            spec_file = spec["spec_path"]
        spec_path = v.normalize_spec_path(spec_file, case)
        if confirm:
            return self.run_spec(spec_path, case, timeout, target)
        return self.preview_spec(spec_path, case)

    # ------------------------------------------------------------ 业务
    def generate_spec(self, text, case, template=None):
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        spec_path = "%s/workspace/%s/pluto/specs/spec-%s.json" % (WORKSPACE, case, stamp)
        args = ["parse", "--text", text, "--case", case, "--out", spec_path]
        if template:
            args += ["--template", template]
        rc, out, err = _engine_call(args, timeout=60)
        data = _json_from(out)
        if data is None:
            return {"ok": False, "error": "引擎解析失败", "rc": rc, "stderr": err[-600:]}
        if "schema_version" not in data:
            data["ok"] = False
            return data
        data["ok"] = True
        data["spec_path"] = spec_path
        data["defaults_used"] = data.get("trace", {}).get("defaults", [])
        return data

    def preview_spec(self, spec_path, case):
        rc, out, err = _engine_call(
            ["run", "--spec-file", spec_path, "--case", case, "--summary"], timeout=120)
        data = _json_from(out)
        if data is None:
            return {"ok": False, "error": "预览失败", "rc": rc, "stderr": err[-600:]}
        data["spec_path"] = spec_path
        return data

    def run_spec(self, spec_path, case, timeout, target):
        v.validate_confirm(True)
        args = ["run", "--spec-file", spec_path, "--case", case,
                "--confirm", "--summary", "--timeout", str(timeout)]
        if target:
            args += ["--target-json", json.dumps(target, ensure_ascii=False)]
        rc, out, err = _engine_call(args, timeout=timeout + 60)
        data = _json_from(out)
        if data is None:
            return {"ok": False, "error": "执行失败", "rc": rc, "stderr": err[-800:]}
        data["spec_path"] = spec_path
        return data


# ---------------------------------------------------------------- 引擎通道
def _bridge_alive():
    try:
        with urllib.request.urlopen(BRIDGE_HEALTH, timeout=3) as r:
            return bool(json.load(r).get("ok"))
    except Exception:
        return False


def _engine_call(args, timeout):
    """优先本地子进程；否则经命令桥。返回 (rc, stdout, stderr)。"""
    if os.path.isfile(ENGINE):
        cmd = [sys.executable, ENGINE] + args
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            return -1, "", "engine timeout %ds" % timeout
    if not _bridge_alive():
        return -1, "", "引擎不可用且命令桥 127.0.0.1:8765 未启动"
    cmd = "python3 %s %s" % (ENGINE, " ".join(shlex.quote(a) for a in args))
    body = json.dumps({"cmd": cmd, "timeout": timeout}).encode("utf-8")
    req = urllib.request.Request(BRIDGE, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout + 30) as r:
            resp = json.load(r)
        return int(resp.get("rc", 0)), resp.get("stdout", ""), resp.get("stderr", "")
    except Exception as exc:
        return -1, "", "bridge error: %s" % exc


def _json_from(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print("SKILL_RESULT=" + json.dumps(
        {k: data.get(k) for k in ("ok", "case", "run_id", "template", "spec_path", "run_dir")},
        ensure_ascii=False))


# ---------------------------------------------------------------- 子命令
def cmd_templates(a):
    rc, out, err = _engine_call(["templates"], timeout=30)
    data = _json_from(out)
    if data is None:
        _emit({"ok": False, "error": "无法读取模板", "rc": rc, "stderr": err[-600:]})
        return 2
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_spec(a):
    try:
        text = v.validate_text(a.text)
        case = v.validate_case(a.case)
        data = JijiangTextToPluto().generate_spec(text, case, a.template)
    except v.ValidationError as exc:
        _emit({"ok": False, "error": str(exc)})
        return 2
    _emit(data)
    return 0 if data.get("ok") else 1


def cmd_preview(a):
    try:
        case = v.validate_case(a.case)
        sp = v.normalize_spec_path(a.spec_file, case)
        data = JijiangTextToPluto().preview_spec(sp, case)
    except v.ValidationError as exc:
        _emit({"ok": False, "error": str(exc)})
        return 2
    _emit(data)
    return 0 if data.get("ok") else 1


def cmd_run(a):
    try:
        case = v.validate_case(a.case)
        sp = v.normalize_spec_path(a.spec_file, case)
        v.validate_confirm(a.confirm)
        timeout = v.validate_timeout(a.timeout)
        target = v.validate_target(a.target_json)
        data = JijiangTextToPluto().run_spec(sp, case, timeout, target)
    except v.ValidationError as exc:
        _emit({"ok": False, "error": str(exc)})
        return 2
    _emit(data)
    return 0 if data.get("ok") else 1


def cmd_status(a):
    try:
        case = v.validate_case(a.case)
    except v.ValidationError as exc:
        _emit({"ok": False, "error": str(exc)})
        return 2
    rc, out, err = _engine_call(["status", "--case", case, "--summary"], timeout=30)
    data = _json_from(out)
    if data is None:
        _emit({"ok": False, "error": "读取状态失败", "rc": rc, "stderr": err[-600:]})
        return 2
    _emit(data)
    return 0 if data.get("ok") else 1


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(prog="jijiang-text-to-pluto",
                                 description="A6 text-to-PLUTO 技能")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("templates", help="列出模板").set_defaults(fn=cmd_templates)

    p = sub.add_parser("spec", help="自然语言 -> spec")
    p.add_argument("--text", required=True)
    p.add_argument("--case", required=True)
    p.add_argument("--template", default=None)
    p.set_defaults(fn=cmd_spec)

    p = sub.add_parser("preview", help="dry-run 渲染 pluto.ini")
    p.add_argument("--spec-file", required=True)
    p.add_argument("--case", required=True)
    p.set_defaults(fn=cmd_preview)

    p = sub.add_parser("run", help="执行 PLUTO 算例")
    p.add_argument("--spec-file", required=True)
    p.add_argument("--case", required=True)
    p.add_argument("--confirm", action="store_true")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--target-json", default=None)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("status", help="最近一次结果")
    p.add_argument("--case", required=True)
    p.set_defaults(fn=cmd_status)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
