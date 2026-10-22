#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_t2pluto.py — Text-to-PLUTO 离线单元测试（可选真实短算例）。

用法：
  python3 test_t2pluto.py           # 离线：解析/校验/渲染/回读/指标提取
  python3 test_t2pluto.py --smoke   # 额外跑一次 5e-10 s 的短算例（真实 PLUTO）
"""
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pluto_runner  # noqa: E402
import t2pluto  # noqa: E402

FAIL = []


def check(label, cond, detail=""):
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", label,
                        ("  " + str(detail)) if detail and not cond else ""))
    if not cond:
        FAIL.append(label)


def close(a, b, rel=1e-9):
    return abs(a - b) <= rel * max(1.0, abs(a), abs(b))


def expect_error(label, fn):
    try:
        fn()
    except t2pluto.SpecError as exc:
        print("[PASS] %s（拒绝：%s）" % (label, str(exc)[:90]))
        return
    check(label, False, "未被拒绝")


MULTI_TEXT = (
    "构建一个 1D 球形多层靶丸算例：DT 燃料内半径 0.15 mm，金层外半径 0.155 mm，"
    "铜层外半径 0.16 mm，燃料层外半径 0.9 mm，泡沫层外半径 0.95 mm，"
    "铍层外半径 1.0 mm，DT 密度 0.2 g/cm3，初始温度 300 K，"
    "模拟到 0.2 ns，网格 2000，计算域 1 mm，外边界流出。"
)

RAD_TEXT = (
    "设计一个 1D 球形灰体辐射驱动内爆算例：DT 燃料半径 1.5 mm，烧蚀层外半径 1.7 mm，"
    "DT 密度 0.007 g/cm3，铝密度 2.7 g/cm3，初始温度 300 K，foot 温度 80 eV，"
    "峰值驱动温度 255 eV，8 ns 开始爬升，12 ns 到达峰值，"
    "模拟到 16 ns，网格 320，计算域 5 mm。"
)


def test_units():
    check("1 mm = 0.1 cm", close(t2pluto.to_canonical(1, "mm")[0], 0.1))
    check("80 eV = 928361 K", close(t2pluto.to_canonical(80, "eV")[0], 928361.45, 1e-6))
    check("1 kg/m3 = 1e-3 g/cm3", close(t2pluto.to_canonical(1, "kg/m3")[0], 1e-3))
    check("1 ns = 1e-9 s", close(t2pluto.to_canonical(1, "ns")[0], 1e-9))
    check("1 GPa = 1e10 dyn/cm2", close(t2pluto.to_canonical(1, "GPa")[0], 1e10))


def test_parse_multilayer():
    spec = t2pluto.parse_text(MULTI_TEXT, case="t2p-multi")
    p = spec["params"]
    check("模板识别 = multilayer", spec["template"] == "icf-multilayer-1d", spec["template"])
    check("R_INNER_DT=0.015 cm", close(p["R_INNER_DT"], 0.015), p["R_INNER_DT"])
    check("R_GOLD=0.0155 cm", close(p["R_GOLD"], 0.0155), p["R_GOLD"])
    check("R_COPPER=0.016 cm", close(p["R_COPPER"], 0.016), p["R_COPPER"])
    check("R_FUEL_OUTER=0.09 cm", close(p["R_FUEL_OUTER"], 0.09), p["R_FUEL_OUTER"])
    check("R_FOAM=0.095 cm", close(p["R_FOAM"], 0.095), p["R_FOAM"])
    check("R_BERYLLIUM=0.1 cm", close(p["R_BERYLLIUM"], 0.1), p["R_BERYLLIUM"])
    check("RHO_DT=0.2", close(p["RHO_DT"], 0.2), p["RHO_DT"])
    check("TEMP_INIT=300 K", close(p["TEMP_INIT"], 300), p["TEMP_INIT"])
    check("tstop=2e-10 s", close(spec["time"]["tstop"], 2e-10), spec["time"]["tstop"])
    check("n1=2000", spec["grid"]["n1"] == 2000, spec["grid"]["n1"])
    check("x1max=0.1 cm", close(spec["grid"]["x1max"], 0.1), spec["grid"]["x1max"])
    check("x1end=outflow", spec["boundary"]["x1end"] == "outflow", spec["boundary"])


def test_parse_rad():
    spec = t2pluto.parse_text(RAD_TEXT, case="t2p-rad")
    p = spec["params"]
    check("模板识别 = rad1d-grey", spec["template"] == "icf-rad1d-grey", spec["template"])
    check("R_DT=0.15 cm", close(p["R_DT"], 0.15), p["R_DT"])
    check("R_OUT=0.17 cm", close(p["R_OUT"], 0.17), p["R_OUT"])
    check("RHO_DT=0.007", close(p["RHO_DT"], 0.007), p["RHO_DT"])
    check("RHO_AL=2.7", close(p["RHO_AL"], 2.7), p["RHO_AL"])
    check("T_FOOT=80 eV", close(p["T_FOOT"], 928361.45, 1e-6), p["T_FOOT"])
    check("T_PEAK=255 eV", close(p["T_PEAK"], 2959152.09, 1e-6), p["T_PEAK"])
    check("T_RAMP0=8 ns", close(p["T_RAMP0"], 8e-9), p["T_RAMP0"])
    check("T_RAMP1=12 ns", close(p["T_RAMP1"], 12e-9), p["T_RAMP1"])
    check("tstop=16 ns", close(spec["time"]["tstop"], 1.6e-8), spec["time"]["tstop"])
    check("x1max=0.5 cm", close(spec["grid"]["x1max"], 0.5), spec["grid"]["x1max"])
    check("radiation solver=hllc", spec["solver"].get("radiation") == "hllc", spec["solver"])


def test_render():
    reg = t2pluto.load_registry()
    for text, case in ((MULTI_TEXT, "t2p-multi"), (RAD_TEXT, "t2p-rad")):
        spec = t2pluto.parse_text(text, case=case)
        tpl_ini = t2pluto.template_ini(spec["template"], reg).read_text(encoding="utf-8")
        rendered = t2pluto.render_ini(spec, tpl_ini, reg)
        info = t2pluto.verify_rendered_ini(rendered, spec, reg)
        check("%s ini 回读校验" % spec["template"], info["ok"], info)
        check("%s 保留模板注释段" % spec["template"],
              "[Grid]" in rendered and "[Parameters]" in rendered)
        check("%s 无注入残留" % spec["template"], "rm -rf" not in rendered)
        check("%s 只生成最终帧" % spec["template"],
              ("vtk        %s  -1   single_file" % t2pluto._fmt(spec["time"]["tstop"])) in rendered
              or ("vtk        %s -1   single_file" % t2pluto._fmt(spec["time"]["tstop"])) in rendered)


def test_rejections():
    expect_error("未知参数拒绝", lambda: t2pluto.parse_text("R_UNKNOWN=1 的算例", case="c1"))
    expect_error("锁定参数拒绝", lambda: t2pluto.parse_text("RHO_GOLD=5 g/cm3", case="c1"))
    expect_error("量纲不匹配拒绝", lambda: t2pluto.parse_text("DT 密度 0.2 cm", case="c1"))
    expect_error("未知模板拒绝", lambda: t2pluto.parse_text(
        "template=not-a-template 的算例", case="c1"))
    expect_error("越界参数拒绝", lambda: t2pluto.parse_text(
        "DT 密度 100 g/cm3，初始温度 300 K", case="c1"))
    expect_error("半径顺序拒绝", lambda: t2pluto.normalize_spec({
        "template": "icf-multilayer-1d", "case": "c1",
        "params": {"R_INNER_DT": 0.03, "R_GOLD": 0.02},
    }))
    expect_error("未知顶层字段拒绝", lambda: t2pluto.normalize_spec({
        "template": "icf-multilayer-1d", "case": "c1", "shell": "rm -rf /"}))
    expect_error("非法 case 拒绝", lambda: t2pluto.normalize_spec({
        "template": "icf-multilayer-1d", "case": "../escape"}))


def test_extract():
    vtk = (t2pluto.workspace_root() / "workspace" / "coil-demo" / "scan"
           / "lhs_runs" / "run_0" / "data.0001.vtk")
    if not vtk.is_file():
        print("[SKIP] 指标提取：样本 vtk 不存在")
        return
    metrics = pluto_runner._extract_metrics(vtk)
    check("vtk 指标有限", math.isfinite(metrics["max_rho"]) and metrics["max_rho"] > 0)
    check("logp 可计算", math.isfinite(metrics["max_logp"]))
    check("温度标注未标定", metrics["max_T_calibrated"] is False)


def test_smoke():
    spec = t2pluto.parse_text(MULTI_TEXT, case="t2p-smoke")
    spec["grid"]["n1"] = 200
    spec["time"]["tstop"] = 5.0e-10
    spec["time"]["first_dt"] = 5.0e-12
    result = pluto_runner.run_case(spec, dry_run=False, timeout_s=180)
    check("真实短算例 rc=0", result["gates"]["solver_rc"]["pass"], result["gates"]["solver_rc"])
    check("真实短算例生成终态 vtk", result["gates"]["final_vtk"]["pass"], result["gates"]["final_vtk"])
    check("真实短算例指标有限", result["gates"]["metrics_finite"]["pass"], result["gates"]["metrics_finite"])
    print("      run_dir = %s" % result.get("run_dir"))
    if result.get("metrics"):
        print("      max_rho=%s max_logp=%s" % (
            result["metrics"]["max_rho"], result["metrics"]["max_logp"]))


def main():
    test_units()
    test_parse_multilayer()
    test_parse_rad()
    test_render()
    test_rejections()
    test_extract()
    if "--smoke" in sys.argv:
        test_smoke()
    print("T2PLUTO_TEST=%s (%d failures)" % ("pass" if not FAIL else "fail", len(FAIL)))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
