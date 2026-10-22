#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jijiang-text-to-pluto — test_anatomy.py（解剖测试）

不调用 PLUTO、不访问网络：只校验 SKILL.md 声明、anatomy()、输入校验与夹具。
输出 ANATOMY_TEST=pass 即通过。
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, ".."))

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %-42s %s" % ("✓" if cond else "✗", name, detail))
    if not cond:
        FAILS.append(name)


def expect_raise(name, fn, exc_type):
    try:
        fn()
    except exc_type:
        check(name, True)
        return
    check(name, False, "未抛异常")


def main():
    print("=" * 74)
    print("jijiang-text-to-pluto 解剖测试")
    print("=" * 74)

    sk_path = os.path.join(_HERE, "..", "SKILL.md")
    sk = open(sk_path, encoding="utf-8").read()
    for sec in ("## Anatomy", "## Inputs", "## Outputs", "## Dependencies",
                "## State", "## Permissions", "## 验收门"):
        check("SKILL.md 含 %s" % sec, sec in sk)

    import main as m
    import validate as v

    a = m.JijiangTextToPluto().anatomy()
    check("anatomy().name 与技能名一致", a["name"] == "jijiang-text-to-pluto", a["name"])
    check("interface = RadHydro.PLUTO.Run", a["interface"] == "RadHydro.PLUTO.Run")
    check("输入含 text/spec_file/case/confirm",
          all(k in a["inputs"] for k in ("text", "spec_file", "case", "confirm")))
    check("输出含 ok/metrics/gates", all(k in a["outputs"] for k in ("ok", "metrics", "gates")))
    check("声明只读 PLUTO 模板", "/mnt/f/PLUTO" in str(a["permissions"]))

    check("合法 case", v.validate_case("demo-01") == "demo-01")
    for bad in ("", "../escape", "a/b", "a b"):
        expect_raise("非法 case 拒绝 %r" % bad, lambda b=bad: v.validate_case(b), v.ValidationError)

    check("合法 text 通过", v.validate_text("  1D 球形多层靶丸  ") .strip() == "1D 球形多层靶丸")
    expect_raise("空 text 拒绝", lambda: v.validate_text("   "), v.ValidationError)
    expect_raise("超长 text 拒绝", lambda: v.validate_text("x" * 2001), v.ValidationError)

    p1 = v.normalize_spec_path(
        r"F:\FusionWorkspace\workspace\demo\pluto\specs\s.json", "demo")
    check("Windows 路径转 WSL", p1 == "/mnt/f/FusionWorkspace/workspace/demo/pluto/specs/s.json", p1)
    expect_raise("跨 case 路径拒绝", lambda: v.normalize_spec_path(
        "/mnt/f/FusionWorkspace/workspace/other/pluto/specs/s.json", "demo"), v.ValidationError)
    expect_raise("路径穿越拒绝", lambda: v.normalize_spec_path(
        "/mnt/f/FusionWorkspace/workspace/demo/pluto/specs/../x.json", "demo"), v.ValidationError)
    expect_raise("非 json 拒绝", lambda: v.normalize_spec_path(
        "/mnt/f/FusionWorkspace/workspace/demo/pluto/specs/s.txt", "demo"), v.ValidationError)

    expect_raise("未确认执行拒绝", lambda: v.validate_confirm(False), v.ValidationError)
    check("确认执行通过", v.validate_confirm(True) is True)
    check("timeout 合法", v.validate_timeout(600) == 600)
    expect_raise("timeout 越界拒绝", lambda: v.validate_timeout(5), v.ValidationError)
    check("target 合法", v.validate_target({"max_rho": [16, 18]}) == {"max_rho": [16, 18]})
    expect_raise("target 未知键拒绝", lambda: v.validate_target({"foo": 1}), v.ValidationError)
    expect_raise("target 区间倒置拒绝", lambda: v.validate_target({"max_rho": [18, 16]}), v.ValidationError)

    fx_path = os.path.join(_HERE, "..", "assets", "fixtures", "test_specs.json")
    fx = json.load(open(fx_path, encoding="utf-8"))
    for case in fx["cases"]:
        try:
            v.validate_spec_shape(case["spec"])
            ok = True
        except v.ValidationError:
            ok = False
        check("夹具[%s] 期望 %s" % (case["name"], "pass" if case["expect"] else "fail"),
              ok == case["expect"])

    check("JSON 提取（带日志前缀）",
          m._json_from('log line\n{"ok": true}\n') == {"ok": True})
    check("截断 JSON 返回 None", m._json_from('{"ok": tru') is None)

    print("-" * 74)
    if FAILS:
        print("ANATOMY_TEST=fail  (%d 项失败: %s)" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("ANATOMY_TEST=pass  （SKILL.md/anatomy/validate/fixtures 全部通过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
