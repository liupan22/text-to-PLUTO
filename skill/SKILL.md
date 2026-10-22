---
name: jijiang-text-to-pluto
description: 中科院的 A6 辐射流体 Agent 的 text-to-PLUTO 技能。自然语言算例描述 → 受控 PLUTO spec → 独立算例目录中的 pluto.ini → 调用已编译 PLUTO 二进制 → 提取终态 max_rho/max_logp → 验收门与结果 JSON。当用户要求"建一个 PLUTO 算例/生成 pluto.ini/跑辐射流体/多层靶丸内爆/灰体辐射驱动/text-to-PLUTO"时使用。
---

# jijiang-text-to-pluto — 自然语言 PLUTO 算例生成技能

OpenClaw Skill Anatomy 规范实现。引擎（单位归一/白名单校验/ini 渲染/执行/指标提取）在
`F:\FusionAgentHub\pluto`，本技能是编排层：负责参数校验、确认门、经命令桥调用引擎。

**能力边界（v1）**：只允许在注册模板上修改白名单参数，**不生成任意 C 代码或 definitions.h，不重新编译 PLUTO**。这是为了确保算例可复现、可审计，而不是限制物理设计空间。

## Anatomy

- **Interface**: `RadHydro.PLUTO.Run`
- **Risk Level**: high（执行已编译求解器并写大文件；仅在用户确认后运行）
- **Parallelizable**: false（单二进制、单算例串行）
- **Avg Duration**: 30s（短算例）/ 600s（完整算例）

## Inputs

| Name | Type | Required | Description |
|---|---|---|---|
| text | string | yes/no | 自然语言算例描述（与 `spec_file` 二选一） |
| spec_file | string | yes/no | 已审阅的 spec JSON 路径（必须在 `F:\FusionWorkspace\workspace\<case>\pluto\` 下） |
| case | string | yes | 算例名，字母/数字/点/下划线/连字符，1-100 位 |
| template | string | no | 模板名：`icf-multilayer-1d` / `icf-rad1d-grey`；默认按文本自动识别 |
| confirm | bool | yes（run） | 必须为 true 才真正执行；否则只做 dry-run 预览 |
| timeout | int | no | 求解超时秒数，默认 600，范围 10-3600 |
| target | object | no | 验收目标，如 `{"max_rho":[16,18],"max_logp":[10.2,10.6]}` |

## Outputs

| Name | Type | Description |
|---|---|---|
| ok | bool | 全部验收门通过 |
| case / run_id / template | string | 算例标识 |
| spec_path | string | 归一化 spec 的落盘路径 |
| pluto_ini | string | dry-run 预览的完整 pluto.ini |
| metrics | object | `max_rho` / `max_p_over_rho` / `max_logp` / `max_T_K`（温度未标定） |
| gates | object | solver_rc / param_injection / final_vtk / metrics_finite / target |
| run_dir | string | 独立算例目录（pluto.ini / pluto_spec.json / run.log / result.json / data.*.vtk） |

## Dependencies

- `fusion-agent-bridge`（http://127.0.0.1:8765，必需；转发到 WSL Ubuntu）
- PLUTO 已编译二进制 `F:\PLUTO\scan\runs\run_60\pluto`、`F:\PLUTO\T006_rad_1D\pluto`（必需）
- 引擎 `F:\FusionAgentHub\pluto\pluto_runner.py`（必需）
- 指标提取器 `F:\FusionWorkspace\scripts\pluto_vtk_extract.py`（必需）

## State

- **Requires**: 注册模板的 pluto.ini 与已编译二进制
- **Creates**: `F:\FusionWorkspace\workspace\<case>\pluto\specs\*.json`、`...\runs\<run_id>\`（pluto.ini / pluto_spec.json / pluto / run.log / data.*.vtk / result.json）
- **Modifies**: `...\pluto\latest.json`（每次运行覆盖为最新结果）
- **Never touches**: `F:\PLUTO\**`（只读模板与二进制）

## Permissions

- **Network**: 仅本机回环 `127.0.0.1:8765`
- **Filesystem**: 写 `F:\FusionWorkspace\workspace\<case>\pluto\`；读 `F:\PLUTO\`、`F:\FusionWorkspace\scripts\`
- **Confirmation Required**: 是（`run` 必须显式 `confirm=true`）

## 使用流程

```bash
# 1) 自然语言 → spec（不执行，输出归一化参数与默认值来源）
python3 main.py spec --text "1D 球形多层靶丸：DT 燃料内半径 0.15 mm，..." --case demo

# 2) 渲染 pluto.ini 预览（dry-run，不落 run 目录）
python3 main.py preview --spec-file /mnt/f/FusionWorkspace/workspace/demo/pluto/specs/xxx.json --case demo

# 3) 用户确认后执行
python3 main.py run --spec-file /mnt/f/.../xxx.json --case demo --confirm --timeout 600
```

命令桥等价于：

```bash
python3 /mnt/f/FusionAgentHub/pluto/pluto_runner.py run \
  --spec-file /mnt/f/FusionWorkspace/workspace/demo/pluto/specs/xxx.json \
  --case demo --confirm --summary
```

## 子命令

| 子命令 | 说明 |
|---|---|
| `templates` | 列出模板、白名单参数与锁定值 |
| `spec --text --case` | 自然语言解析 + 单位归一 + 默认值补全，落盘 spec |
| `preview --spec-file --case` | 只渲染 pluto.ini 并回读校验 |
| `run --spec-file --case --confirm` | 真正执行并提取指标 |
| `status --case` | 读最近一次 `latest.json` |

## 验收门（全部通过才 ok=true）

1. `solver_rc`：PLUTO 返回 0 且未超时
2. `param_injection`：回读 pluto.ini 校验每个参数与 spec 一致（相对误差 ≤1e-9）
3. `final_vtk`：存在终态 `data.*.vtk`
4. `metrics_finite`：`max_rho > 0`、`max_logp` 有限
5. `target`：提供 target 时逐项判定

## 失败处理

- 解析/校验失败：返回 `{"ok": false, "error": "..."}`，不创建 run 目录。
- 求解失败：保留 run.log 与 pluto.ini，`gates.solver_rc=false`，不自动重试。
- 指标非有限：`gates.metrics_finite=false`，不进入 A1 登记。
- **温度说明**：`max_T_K = 120.3 * max_p_over_rho * mu_factor`，`max_T_calibrated=false`，不得用于热力学结论。

## 解剖测试

```bash
python3 /mnt/e/OpenClaw/skills/jijiang-text-to-pluto/scripts/test_anatomy.py
```

输出 `ANATOMY_TEST=pass` 表示 SKILL.md 声明、anatomy()、输入校验与夹具一致，全程不调用求解器。
