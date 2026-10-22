#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""t2pluto.py — Text-to-PLUTO 编译核心。

职责（纯离线、无副作用）：
  1. 自然语言 → 受控 PLUTO spec（单位归一、模板识别、白名单参数抽取）
  2. spec JSON → 校验 + 默认值补全（不生成任意 C/definitions.h）
  3. spec → pluto.ini 渲染与回读校验（只改白名单键，其余原文保留）

真实求解与指标提取在 pluto_runner.py；本模块可单独用于 dry-run 和测试。
"""
import json
import math
import os
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
REGISTRY_PATH = HERE / "templates.json"
SCHEMA_VERSION = "1.0"
CASE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
TEXT_MIN_LENGTH = 4
TEXT_MAX_LENGTH = 2000


class SpecError(ValueError):
    """spec 不可满足或越界。"""


# ---------------------------------------------------------------- 路径
def pluto_root():
    if os.name == "nt":
        return Path(os.environ.get("PLUTO_ROOT", r"F:\PLUTO"))
    return Path(os.environ.get("PLUTO_ROOT", "/mnt/f/PLUTO"))


def workspace_root():
    if os.name == "nt":
        return Path(os.environ.get("FUSION_WORKSPACE", r"F:\FusionWorkspace"))
    return Path(os.environ.get("FUSION_WORKSPACE", "/mnt/f/FusionWorkspace"))


def load_registry():
    with open(REGISTRY_PATH, encoding="utf-8") as f:
        return json.load(f)


def template_binary(name, registry=None):
    reg = registry or load_registry()
    return pluto_root() / reg["templates"][name]["binary"]


def template_ini(name, registry=None):
    reg = registry or load_registry()
    return pluto_root() / reg["templates"][name]["ini"]


# ---------------------------------------------------------------- 单位
# 全部换算到 PLUTO/cgs 基准：cm, g/cm^3, s, K, dyn/cm^2
UNIT_FACTORS = {
    "length": {
        "cm": 1.0, "厘米": 1.0,
        "m": 100.0, "米": 100.0,
        "mm": 0.1, "毫米": 0.1,
        "um": 1.0e-4, "µm": 1.0e-4, "μm": 1.0e-4, "微米": 1.0e-4,
        "nm": 1.0e-7, "纳米": 1.0e-7,
        "km": 1.0e5, "千米": 1.0e5,
    },
    "density": {
        "g/cm3": 1.0, "g/cm^3": 1.0, "g/cc": 1.0, "克/立方厘米": 1.0,
        "kg/m3": 1.0e-3, "kg/m^3": 1.0e-3, "千克/立方米": 1.0e-3,
    },
    "time": {
        "s": 1.0, "秒": 1.0,
        "ms": 1.0e-3, "毫秒": 1.0e-3,
        "us": 1.0e-6, "µs": 1.0e-6, "μs": 1.0e-6, "微秒": 1.0e-6,
        "ns": 1.0e-9, "纳秒": 1.0e-9,
        "ps": 1.0e-12, "皮秒": 1.0e-12,
        "fs": 1.0e-15, "飞秒": 1.0e-15,
    },
    "temperature": {
        "k": 1.0, "开": 1.0, "开尔文": 1.0,
        "ev": 11604.51812, "电子伏": 11604.51812, "电子伏特": 11604.51812,
        "kev": 1.160451812e7, "千电子伏": 1.160451812e7,
    },
    "pressure": {
        "dyn/cm2": 1.0, "dyn/cm^2": 1.0,
        "pa": 10.0, "帕": 10.0,
        "kpa": 1.0e4, "千帕": 1.0e4,
        "mpa": 1.0e7, "兆帕": 1.0e7,
        "gpa": 1.0e10, "吉帕": 1.0e10,
        "bar": 1.0e6, "巴": 1.0e6,
    },
}
_UNIT_TO_DIM = {}
for _dim, _table in UNIT_FACTORS.items():
    for _u in _table:
        _UNIT_TO_DIM[_u.lower()] = _dim
_UNIT_TOKENS = sorted(
    {u for table in UNIT_FACTORS.values() for u in table},
    key=len, reverse=True,
)
_NUM = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_UNIT_RE = "|".join(re.escape(u) for u in _UNIT_TOKENS)
QUANTITY_RE = re.compile(r"(?<![\w.])(" + _NUM + r")\s*(" + _UNIT_RE + r")?", re.I)


def unit_dimension(unit):
    if not unit:
        return None
    return _UNIT_TO_DIM.get(unit.lower())


def to_canonical(value, unit):
    """返回 (cgs 值, 维度)。无单位时维度为 None。"""
    dim = unit_dimension(unit)
    if dim is None:
        return float(value), None
    return float(value) * UNIT_FACTORS[dim][unit.lower()], dim


# ---------------------------------------------------------------- 文本解析
def _clause_before(text, pos, window=48):
    start = max(0, pos - window)
    clause = text[start:pos]
    cut = max((clause.rfind(c) for c in "，。；;、,：:\n"), default=-1)
    return clause[cut + 1:]


def _clause_after(text, pos, window=32):
    clause = text[pos:pos + window]
    cut = min((clause.find(c) for c in "，。；;、,：:\n" if clause.find(c) >= 0),
              default=len(clause))
    return clause[:cut]


def _norm(s):
    """匹配时忽略空白，兼容 “DT 燃料半径 / DT燃料半径”。"""
    return re.sub(r"\s+", "", s.lower())


def _match_alias(clause, aliases):
    low = _norm(clause)
    best, best_end = None, -1
    for alias in aliases:
        idx = low.rfind(_norm(alias))
        if idx >= 0 and idx + len(_norm(alias)) > best_end:
            best, best_end = alias, idx + len(_norm(alias))
    return best


def _match_param(clause, tpl):
    """在子句中找最近出现的白名单参数别名，返回参数键。"""
    best, best_end = None, -1
    low = _norm(clause)
    for key, aliases in _all_param_aliases(tpl).items():
        for alias in aliases:
            idx = low.rfind(_norm(alias))
            if idx >= 0 and idx + len(_norm(alias)) > best_end:
                best, best_end = key, idx + len(_norm(alias))
    return best


_RAMP_ALIASES = {
    "T_RAMP0": ["开始爬升", "开始上升", "爬升开始", "rampstart"],
    "T_RAMP1": ["到达峰值", "达到峰值", "升至峰值", "rampend"],
}


def _match_ramp(clause, tpl):
    low = _norm(clause)
    best, best_end = None, -1
    for key, aliases in _RAMP_ALIASES.items():
        if key not in tpl["parameters"]:
            continue
        for alias in aliases:
            idx = low.rfind(_norm(alias))
            if idx >= 0 and idx + len(_norm(alias)) > best_end:
                best, best_end = key, idx + len(_norm(alias))
    return best


def detect_template(text, registry=None):
    reg = registry or load_registry()
    m = re.search(r"(?:template|模板)\s*[=:]\s*([A-Za-z0-9._-]+)", text, re.I)
    if m:
        name = m.group(1)
        if name not in reg["templates"]:
            raise SpecError("未知模板 %r；可用：%s" % (name, ", ".join(reg["templates"])))
        return name
    scores = {}
    for name, tpl in reg["templates"].items():
        hits = [kw for kw in tpl.get("text_keywords", []) if kw.lower() in text.lower()]
        scores[name] = sum(len(kw) for kw in hits)
    best = max(scores, key=scores.get)
    if scores[best] <= 0:
        return "icf-multilayer-1d"
    return best


_SPECIAL_ASSIGN = {
    "TSTOP": ("time", "tstop"),
    "CFL": ("time", "cfl"),
    "FIRST_DT": ("time", "first_dt"),
    "N1": ("grid", "n1"),
    "X1-GRID": ("grid", "n1"),
    "X1MAX": ("grid", "x1max"),
    "VTK_INTERVAL": ("output", "vtk_interval"),
}

_SPECIAL_ALIASES = {
    ("time", "tstop"): ["终止时间", "结束时间", "模拟时长", "模拟到", "总时长", "物理时间", "跑到", "tstop", "end time", "simulation time"],
    ("time", "cfl"): ["库朗数", "cfl", "courant"],
    ("time", "first_dt"): ["初始步长", "first_dt", "initial dt"],
    ("grid", "n1"): ["网格点数", "网格数", "网格", "分辨率", "点数", "nx", "n1", "grid points", "resolution"],
    ("grid", "x1max"): ["计算域半径", "计算域", "域半径", "外边界", "x1max", "domain radius", "domain size"],
    ("output", "vtk_interval"): ["输出间隔", "vtk间隔", "写帧间隔", "vtk_interval", "output interval"],
}

_SOLVER_TOKENS = {
    "hllc": "hllc", "hll": "hll", "roe": "roe", "tvdlf": "tvdlf",
    "hllc求解器": "hllc",
}
_BOUNDARY_TOKENS = {
    "reflective": "reflective", "反射": "reflective", "固壁": "reflective",
    "outflow": "outflow", "流出": "outflow", "自由出流": "outflow", "开放": "outflow",
    "userdef": "userdef", "用户定义": "userdef",
}


def parse_text(text, template=None, case=None, registry=None):
    """自然语言 → 归一化 spec。解析失败抛 SpecError。"""
    reg = registry or load_registry()
    if not isinstance(text, str):
        raise SpecError("text 必须是字符串")
    text = text.strip()
    if not TEXT_MIN_LENGTH <= len(text) <= TEXT_MAX_LENGTH:
        raise SpecError("text 长度必须为 %d-%d 个字符" % (TEXT_MIN_LENGTH, TEXT_MAX_LENGTH))
    if any(ord(c) < 32 and c not in "\t\n\r" for c in text):
        raise SpecError("text 含不允许的控制字符")

    tpl_name = template or detect_template(text, reg)
    tpl = reg["templates"].get(tpl_name)
    if tpl is None:
        raise SpecError("未知模板 %r" % tpl_name)

    raw = {
        "schema_version": SCHEMA_VERSION,
        "case": case or "t2p-auto",
        "template": tpl_name,
        "text": text,
        "params": {},
        "grid": {},
        "time": {},
        "solver": {},
        "boundary": {},
        "output": {},
        "assumptions": [],
        "trace": {"explicit": [], "defaults": [], "unassigned_numbers": []},
    }

    consumed = []
    # 1) 显式 key=value（最高优先级，也用于避免重复抽取）
    for m in re.finditer(r"(?<![\w])([A-Za-z][A-Za-z0-9_-]{1,30})\s*[=:]\s*(" + _NUM + r")\s*(" + _UNIT_RE + r")?", text, re.I):
        key = m.group(1).upper()
        value, unit = float(m.group(2)), m.group(3)
        consumed.append((m.start(), m.end()))
        if key in tpl["parameters"]:
            p = tpl["parameters"][key]
            val, dim = to_canonical(value, unit)
            _check_dimension(key, p.get("unit"), dim)
            if dim is None and p.get("unit"):
                raw["assumptions"].append(
                    "参数 %s 未带单位，按模板 cgs 单位（%s）解释" % (key, p["unit"]))
            raw["params"][key] = val
            raw["trace"]["explicit"].append({"key": key, "value_cgs": val, "unit": unit})
        elif key in tpl.get("fixed_parameters", {}):
            raise SpecError("参数 %s 在模板 %s 中为锁定值 %s，不允许改写；如需改变请扩展后端"
                            % (key, tpl_name, tpl["fixed_parameters"][key]))
        elif key in _SPECIAL_ASSIGN:
            section, field = _SPECIAL_ASSIGN[key]
            val, dim = to_canonical(value, unit)
            raw[section][field] = int(val) if field == "n1" else val
            raw["trace"]["explicit"].append({"key": "%s.%s" % (section, field), "value_cgs": val, "unit": unit})
        else:
            raise SpecError("未知参数 %r；模板 %s 允许：%s"
                            % (key, tpl_name, ", ".join(sorted(tpl["parameters"]))))

    # 2) 数值 + 角色关键词（白名单参数）
    for m in QUANTITY_RE.finditer(text):
        if any(a <= m.start() < b for a, b in consumed):
            continue
        clause = _clause_before(text, m.start())
        follow = _clause_after(text, m.end())
        role = _match_ramp(follow, tpl) or _match_param(clause, tpl)
        if role:
            p = tpl["parameters"][role]
            val, dim = to_canonical(float(m.group(1)), m.group(2))
            _check_dimension(role, p.get("unit"), dim)
            if role in raw["params"]:
                raise SpecError("参数 %s 在文本中重复出现且无法判定优先级" % role)
            if dim is None and p.get("unit"):
                raw["assumptions"].append(
                    "参数 %s 未带单位，按模板 cgs 单位（%s）解释" % (role, p["unit"]))
            raw["params"][role] = val
            raw["trace"]["explicit"].append({"key": role, "value_cgs": val, "unit": m.group(2)})
            continue
        # 3) 特殊字段
        special = _match_special(clause)
        if special:
            section, field = special
            val, dim = to_canonical(float(m.group(1)), m.group(2))
            if field == "n1":
                raw[section][field] = int(round(val))
            else:
                raw[section][field] = val
            if dim is None and field in ("tstop", "first_dt", "x1max", "vtk_interval"):
                raw["assumptions"].append("%s.%s 未带单位，按 cgs 解释" % (section, field))
            raw["trace"]["explicit"].append({"key": "%s.%s" % (section, field), "value_cgs": val, "unit": m.group(2)})
            continue
        raw["trace"]["unassigned_numbers"].append(m.group(0).strip())

    # 4) 求解器 / 边界 / 模板关键字
    low = text.lower()
    for token, value in _SOLVER_TOKENS.items():
        if token in low:
            raw["solver"]["hydro"] = value
            break
    if tpl["solver"].get("radiation"):
        raw["solver"].setdefault("radiation", raw["solver"].get("hydro", tpl["solver"]["radiation"]["default"]))
    for token, value in _BOUNDARY_TOKENS.items():
        if token in low:
            if "x1beg" not in raw["boundary"] and any(k in low for k in ("内边界", "x1-beg", "左边界", "中心")):
                raw["boundary"]["x1beg"] = value
            elif "x1end" not in raw["boundary"] and any(k in low for k in ("外边界", "x1-end", "右边界", "外侧")):
                raw["boundary"]["x1end"] = value
    if "反射" in text and "x1beg" not in raw["boundary"]:
        raw["boundary"]["x1beg"] = "reflective"

    return normalize_spec(raw, registry=reg)


def _all_param_aliases(tpl):
    return {key: p.get("aliases", []) for key, p in tpl["parameters"].items()}


def _match_special(clause):
    best, best_score = None, -1
    low = _norm(clause)
    for field, aliases in _SPECIAL_ALIASES.items():
        for alias in aliases:
            idx = low.rfind(_norm(alias))
            if idx >= 0 and len(_norm(alias)) > best_score:
                best, best_score = field, len(_norm(alias))
    return best


def _check_dimension(key, expected, actual):
    if actual is None or expected is None:
        return
    if actual != expected:
        raise SpecError("参数 %s 需要 %s 量纲，收到 %s" % (key, expected, actual))


# ---------------------------------------------------------------- 校验/默认值
def normalize_spec(spec, registry=None):
    reg = registry or load_registry()
    if not isinstance(spec, dict):
        raise SpecError("spec 必须是 JSON 对象")
    allowed_top = {"schema_version", "case", "template", "text", "params", "grid",
                   "time", "solver", "boundary", "output", "target",
                   "assumptions", "trace", "notes"}
    unknown = set(spec) - allowed_top
    if unknown:
        raise SpecError("spec 含未知字段：%s" % ", ".join(sorted(unknown)))

    tpl_name = spec.get("template")
    if tpl_name not in reg["templates"]:
        raise SpecError("未知模板 %r" % tpl_name)
    tpl = reg["templates"][tpl_name]

    for section, allowed in (("grid", {"n1", "x1max"}),
                             ("time", {"tstop", "cfl", "first_dt"}),
                             ("solver", {"hydro", "radiation"}),
                             ("boundary", {"x1beg", "x1end"}),
                             ("output", {"vtk_interval", "mu_factor"})):
        block = spec.get(section, {}) or {}
        if not isinstance(block, dict):
            raise SpecError("%s 必须是 JSON 对象" % section)
        extra = set(block) - allowed
        if extra:
            raise SpecError("%s 含未知字段：%s" % (section, ", ".join(sorted(extra))))

    case = spec.get("case", "t2p-auto")
    if not isinstance(case, str) or not CASE_PATTERN.fullmatch(case):
        raise SpecError("case 必须是 1-100 位字母、数字、点、下划线或连字符")

    out = {
        "schema_version": SCHEMA_VERSION,
        "case": case,
        "template": tpl_name,
        "text": spec.get("text", ""),
        "params": {},
        "grid": {},
        "time": {},
        "solver": {},
        "boundary": {},
        "output": {},
        "target": spec.get("target"),
        "assumptions": list(spec.get("assumptions", [])),
        "trace": spec.get("trace", {}),
        "notes": spec.get("notes", ""),
    }

    # 参数：模板默认 + 显式覆盖
    explicit_params = dict(spec.get("params", {}))
    for key in explicit_params:
        if key in tpl.get("fixed_parameters", {}):
            raise SpecError("参数 %s 为锁定值，不允许改写" % key)
        if key not in tpl["parameters"]:
            raise SpecError("未知参数 %r；允许：%s" % (key, ", ".join(sorted(tpl["parameters"]))))
    defaults = []
    for key, p in tpl["parameters"].items():
        if key in explicit_params:
            val = _quantity("%s.params.%s" % (tpl_name, key), explicit_params[key], p.get("unit"))
        else:
            val = float(p["default"])
            defaults.append("params.%s=%s" % (key, _fmt(val)))
        lo, hi = p.get("min"), p.get("max")
        if lo is not None and val < lo:
            raise SpecError("参数 %s=%s 小于下限 %s" % (key, _fmt(val), lo))
        if hi is not None and val > hi:
            raise SpecError("参数 %s=%s 大于上限 %s" % (key, _fmt(val), hi))
        out["params"][key] = val

    # grid / time / solver / boundary
    gspec = spec.get("grid", {})
    out["grid"]["n1"] = int(_quantity("grid.n1", gspec.get("n1", tpl["grid"]["n1"]["default"])))
    if not tpl["grid"]["n1"]["min"] <= out["grid"]["n1"] <= tpl["grid"]["n1"]["max"]:
        raise SpecError("grid.n1 超出 [%s, %s]" % (tpl["grid"]["n1"]["min"], tpl["grid"]["n1"]["max"]))
    out["grid"]["x1max"] = _quantity("grid.x1max", gspec.get("x1max", tpl["grid"]["x1max"]["default"]),
                                     tpl["grid"]["x1max"].get("unit"))

    tspec = spec.get("time", {})
    for key in ("tstop", "cfl", "first_dt"):
        out["time"][key] = _quantity("time." + key, tspec.get(key, tpl["time"][key]["default"]),
                                     tpl["time"][key].get("unit"))
        lo, hi = tpl["time"][key]["min"], tpl["time"][key]["max"]
        if not lo <= out["time"][key] <= hi:
            raise SpecError("time.%s 超出 [%s, %s]" % (key, lo, hi))

    sspec = spec.get("solver", {})
    hydro = sspec.get("hydro", tpl["solver"]["hydro"]["default"])
    if hydro not in tpl["solver"]["hydro"]["enum"]:
        raise SpecError("solver.hydro 必须是 %s" % tpl["solver"]["hydro"]["enum"])
    out["solver"]["hydro"] = hydro
    if tpl["solver"].get("radiation"):
        rad = sspec.get("radiation", tpl["solver"]["radiation"]["default"])
        if rad not in tpl["solver"]["radiation"]["enum"]:
            raise SpecError("solver.radiation 必须是 %s" % tpl["solver"]["radiation"]["enum"])
        out["solver"]["radiation"] = rad

    bspec = spec.get("boundary", {})
    for key in ("x1beg", "x1end"):
        val = bspec.get(key, tpl["boundary"][key]["default"])
        if val not in tpl["boundary"][key]["enum"]:
            raise SpecError("boundary.%s 必须是 %s" % (key, tpl["boundary"][key]["enum"]))
        out["boundary"][key] = val

    ospec = spec.get("output", {})
    vtk = ospec.get("vtk_interval")
    out["output"]["vtk_interval"] = (_quantity("output.vtk_interval", vtk, "time")
                                     if vtk is not None else out["time"]["tstop"])
    if not 0 < out["output"]["vtk_interval"] <= out["time"]["tstop"]:
        raise SpecError("output.vtk_interval 必须 >0 且 ≤ time.tstop")
    out["output"]["mu_factor"] = _quantity("output.mu_factor", ospec.get("mu_factor", 1.0))
    if out["output"]["mu_factor"] <= 0:
        raise SpecError("output.mu_factor 必须 > 0")

    # 约束（含网格域约束）
    values = dict(out["params"])
    values.update(tpl.get("fixed_parameters", {}))
    values["x1max"] = out["grid"]["x1max"]
    for lhs, op, rhs in tpl.get("constraints", []):
        a, b = values.get(lhs), values.get(rhs)
        if a is None or b is None:
            raise SpecError("约束引用了未知量：%s %s %s" % (lhs, op, rhs))
        ok = {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b, "==": a == b}[op]
        if not ok:
            raise SpecError("约束不满足：%s(%s) %s %s(%s)" % (lhs, _fmt(a), op, rhs, _fmt(b)))

    # target
    if out["target"] is not None:
        if not isinstance(out["target"], dict):
            raise SpecError("target 必须是 {目标量: [lo, hi] 或 数值}")
        allowed_targets = {"max_rho", "max_p_over_rho", "max_logp", "max_T_K"}
        for key, val in out["target"].items():
            if key not in allowed_targets:
                raise SpecError("target 含未知目标 %r；允许：%s" % (key, ", ".join(sorted(allowed_targets))))
            if isinstance(val, list):
                if len(val) != 2 or not all(_is_number(v) for v in val) or val[0] > val[1]:
                    raise SpecError("target.%s 必须是 [lo, hi] 且 lo<=hi" % key)

    out["trace"]["defaults"] = defaults
    out["assumptions"] = _dedupe(out["assumptions"])
    return out


def _quantity(label, value, dimension=None):
    """数值或 "0.15 mm" 形式的物理量；字符串按单位换算到 cgs。"""
    if _is_number(value):
        return float(value)
    if isinstance(value, str):
        m = re.fullmatch(r"\s*(" + _NUM + r")\s*(" + _UNIT_RE + r")?\s*", value, re.I)
        if not m:
            raise SpecError("%s 不是合法物理量：%r" % (label, value))
        val, dim = to_canonical(float(m.group(1)), m.group(2))
        if dimension and dim and dim != dimension:
            raise SpecError("%s 需要 %s 量纲，收到 %s" % (label, dimension, dim))
        return val
    raise SpecError("%s 必须是数值或带单位字符串" % label)


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def _fmt(v):
    return ("%.12g" % v)


def _dedupe(items):
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ---------------------------------------------------------------- ini 渲染
def render_ini(spec, template_text, registry=None):
    """按 spec 渲染 pluto.ini。只改白名单键，保留注释与其余段落。"""
    reg = registry or load_registry()
    tpl = reg["templates"][spec["template"]]
    template_text = template_text.lstrip("\ufeff")
    params = dict(tpl.get("fixed_parameters", {}))
    params.update(spec["params"])

    out_lines, section = [], ""
    for line in template_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().lower()
            out_lines.append(line)
            continue
        if not stripped:
            out_lines.append(line)
            continue

        key = stripped.split()[0]
        low = key.lower()
        if section == "grid" and low == "x1-grid":
            out_lines.append("X1-grid    1   0.0    %d   u   %s"
                             % (spec["grid"]["n1"], _fmt(spec["grid"]["x1max"])))
        elif section == "time" and low in ("cfl", "tstop", "first_dt"):
            out_lines.append("%-14s %s" % (key, _fmt(spec["time"][low])))
        elif section == "solver" and low in ("solver", "radsolver"):
            if low == "solver":
                out_lines.append("%-14s %s" % (key, spec["solver"]["hydro"]))
            else:
                out_lines.append("%-14s %s" % (key, spec["solver"].get("radiation", spec["solver"]["hydro"])))
        elif section == "boundary" and low in ("x1-beg", "x1-end"):
            field = "x1beg" if low == "x1-beg" else "x1end"
            out_lines.append("%-12s %s" % (key, spec["boundary"][field]))
        elif section == "static grid output":
            if low == "dbl":
                out_lines.append("%-10s -1.0    -1   single_file" % key)
            elif low == "vtk":
                out_lines.append("%-10s %s  -1   single_file"
                                 % (key, _fmt(spec["output"]["vtk_interval"])))
            elif low == "flt":
                out_lines.append("%-10s -1.0    -1   single_file" % key)
            elif low == "tab":
                out_lines.append("%-10s -1.0    -1   multiple_files" % key)
            elif low in ("ppm", "png"):
                out_lines.append("%-10s -1.0    -1" % key)
            elif low == "log":
                out_lines.append("%-10s 100" % key)
            elif low == "analysis":
                out_lines.append("%-10s -1.0    -1" % key)
            else:
                out_lines.append(line)
        elif section == "parameters" and key in params:
            out_lines.append("%-28s %s" % (key, _fmt(params[key])))
        else:
            out_lines.append(line)

    # 补齐模板中缺失的白名单参数
    present = set()
    in_params = False
    for line in out_lines:
        s = line.strip()
        if s.startswith("["):
            in_params = s.lower() == "[parameters]"
        elif in_params and s:
            present.add(s.split()[0])
    missing = [k for k in params if k not in present]
    if missing:
        if "[Parameters]" not in "\n".join(out_lines):
            out_lines += ["", "[Parameters]"]
        for key in missing:
            out_lines.append("%-28s %s" % (key, _fmt(params[key])))

    rendered = "\n".join(out_lines) + "\n"
    try:
        rendered.encode("ascii")
    except UnicodeEncodeError:
        raise SpecError("渲染后的 pluto.ini 含非 ASCII 字符，PLUTO 将拒绝或静默失败")
    return rendered


def verify_rendered_ini(rendered, spec, registry=None):
    """回读 pluto.ini 并核对注入值，防止扫描脚本式的“改串不匹配”。"""
    reg = registry or load_registry()
    tpl = reg["templates"][spec["template"]]
    expected = dict(tpl.get("fixed_parameters", {}))
    expected.update(spec["params"])

    sections = _parse_ini(rendered)
    problems = []
    for key, want in expected.items():
        got = _first(sections.get("parameters", {}).get(key.lower()))
        if got is None:
            problems.append("参数 %s 未写入" % key)
        elif not _close(got, want):
            problems.append("参数 %s 回读 %s ≠ 期望 %s" % (key, _fmt(got), _fmt(want)))
    grid = sections.get("grid", {}).get("x1-grid")
    if not grid or int(grid[2]) != spec["grid"]["n1"] or not _close(grid[4], spec["grid"]["x1max"]):
        problems.append("X1-grid 回读异常：%r" % (grid,))
    tim = sections.get("time", {})
    for key in ("tstop", "cfl", "first_dt"):
        if not _close(_first(tim.get(key)), spec["time"][key]):
            problems.append("time.%s 回读 %r ≠ %s" % (key, tim.get(key), _fmt(spec["time"][key])))
    vtk = sections.get("static grid output", {}).get("vtk")
    if not vtk or not _close(vtk[0], spec["output"]["vtk_interval"]):
        problems.append("vtk 输出间隔回读异常：%r" % (vtk,))
    if problems:
        raise SpecError("ini 注入校验失败：" + "；".join(problems))
    return {"ok": True, "checked_params": len(expected), "n1": spec["grid"]["n1"]}


def _parse_ini(text):
    sections, current = {}, ""
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("[") and s.endswith("]"):
            current = s[1:-1].strip().lower()
            sections.setdefault(current, {})
            continue
        parts = s.split()
        if current and parts:
            sections.setdefault(current, {})[parts[0].lower()] = parts[1:]
    return sections


def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _close(a, b, rel=1e-9):
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return abs(a - b) <= rel * max(1.0, abs(a), abs(b))


def spec_fingerprint(spec):
    import hashlib
    payload = json.dumps(spec, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


if __name__ == "__main__":
    print(json.dumps(load_registry(), ensure_ascii=False, indent=2))
