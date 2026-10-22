#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jijiang-text-to-pluto — validate.py（输入校验，独立于引擎）"""
import json
import math
import posixpath
import re

CASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
TEXT_MIN, TEXT_MAX = 4, 2000
ALLOWED_TARGETS = {"max_rho", "max_p_over_rho", "max_logp", "max_T_K"}
WORKSPACE_WSL = "/mnt/f/FusionWorkspace"
_QUANTITY_RE = re.compile(r"^[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?\s*[A-Za-zµμ/^0-9]*$")


class ValidationError(ValueError):
    pass


def validate_case(case):
    if not isinstance(case, str) or not CASE_RE.fullmatch(case):
        raise ValidationError("case 必须是 1-100 位字母、数字、点、下划线或连字符")
    return case


def validate_text(text):
    if not isinstance(text, str):
        raise ValidationError("text 必须是字符串")
    text = text.strip()
    if not TEXT_MIN <= len(text) <= TEXT_MAX:
        raise ValidationError("text 长度必须为 %d-%d" % (TEXT_MIN, TEXT_MAX))
    if any(ord(c) < 32 and c not in "\t\n\r" for c in text):
        raise ValidationError("text 含不允许的控制字符")
    return text


def normalize_spec_path(path, case):
    """把 Windows/WSL 路径统一成 /mnt/f/... 并限制在 case 的 pluto 目录内。"""
    if not isinstance(path, str) or not path.strip():
        raise ValidationError("spec_file 必须是非空路径")
    p = path.strip().replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    if m:
        p = "/mnt/%s/%s" % (m.group(1).lower(), m.group(2))
    if not p.startswith("/"):
        raise ValidationError("spec_file 必须是绝对路径")
    if ".." in p.split("/"):
        raise ValidationError("spec_file 不允许包含 ..")
    norm = posixpath.normpath(p)
    prefix = "%s/workspace/%s/pluto/" % (WORKSPACE_WSL, case)
    if not norm.startswith(prefix):
        raise ValidationError("spec_file 必须位于 %s 下" % prefix)
    if not norm.endswith(".json"):
        raise ValidationError("spec_file 必须是 .json")
    return norm


def validate_timeout(timeout):
    try:
        t = int(timeout)
    except (TypeError, ValueError):
        raise ValidationError("timeout 必须是整数秒")
    if not 10 <= t <= 3600:
        raise ValidationError("timeout 超出 [10, 3600]")
    return t


def validate_confirm(confirm):
    if confirm is not True:
        raise ValidationError("真正执行必须显式 confirm=true")
    return True


def validate_target(target):
    if target is None:
        return None
    if isinstance(target, str):
        try:
            target = json.loads(target)
        except json.JSONDecodeError as exc:
            raise ValidationError("target 不是合法 JSON：%s" % exc)
    if not isinstance(target, dict):
        raise ValidationError("target 必须是 JSON 对象")
    for key, val in target.items():
        if key not in ALLOWED_TARGETS:
            raise ValidationError("target 含未知目标 %r" % key)
        if isinstance(val, list):
            if len(val) != 2 or not all(_num(v) for v in val) or val[0] > val[1]:
                raise ValidationError("target.%s 必须是 [lo, hi] 且 lo<=hi" % key)
        elif not _num(val):
            raise ValidationError("target.%s 必须是数值或 [lo, hi]" % key)
    return target


def validate_spec_shape(spec):
    """轻量结构校验（深层物理校验由引擎负责）。"""
    if not isinstance(spec, dict):
        raise ValidationError("spec 必须是 JSON 对象")
    if spec.get("schema_version") != "1.0":
        raise ValidationError("schema_version 必须为 1.0")
    validate_case(spec.get("case"))
    if not isinstance(spec.get("template"), str):
        raise ValidationError("template 必须是字符串")
    params = spec.get("params", {})
    if not isinstance(params, dict):
        raise ValidationError("params 必须是对象")
    for key, val in params.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValidationError("参数名 %r 非白名单风格" % key)
        if not (_num(val) or (isinstance(val, str) and _QUANTITY_RE.fullmatch(val))):
            raise ValidationError("参数 %s 必须是有限数值或带单位字符串" % key)
    for section in ("grid", "time", "solver", "boundary", "output"):
        if section in spec and not isinstance(spec[section], dict):
            raise ValidationError("%s 必须是对象" % section)
    return True


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))
