# A6 受控 Text-to-PLUTO 技能实施

## 1. 目标与能力边界

在 A3 text-to-CAD、A5/A8/A9 text-to-solver 之后，为 A6 辐射流体 Agent 增加**自然语言单算例入口**：

```
中文算例描述 → 受控 PLUTO spec → pluto.ini → 已编译 PLUTO → 终态指标 → 验收门 → result.json
```

定位是**一个可复现算例**，用于：

- M6 闭环验证（把帕累托代表解落到真实求解器）；
- 物理假设快速检验（改半径、密度、时间步、驱动温度曲线）；
- 为 LHS 批量扫描**定标边界**。

**不替代** A6 的 300 点扫描链（`lhs_sampler.py + pluto_lhs_scan.sh + surrogate_mv.py`）。

**硬边界（v1）**：

| 边界 | 说明 |
|---|---|
| 模板受限 | 仅 `icf-multilayer-1d`（HD，1D 球形多层靶丸）与 `icf-rad1d-grey`（HD+RADIATION 灰体 FLD） |
| 参数白名单 | 只允许注册表列出的参数；锁定材料密度/常数不可改 |
| 不重编译 | 不生成任意 C / `definitions.h`，不调用 make；只注入 `pluto.ini` |
| 不写模板目录 | `F:\PLUTO\**` 只读；每次运行复制二进制到独立 run 目录 |
| 先预览后执行 | `preview` 只渲染并回读；`run` 必须显式 `--confirm`，否则仅 dry-run |
| 温度不可用 | `max_T_K` 未标定（`max_T_calibrated=false`），只允许使用 `max_rho` / `max_logp` 下结论 |

## 2. 总体架构

```
                     ┌──────────────────────────── OpenClaw Gateway (WSL) ───────────────────────────┐
用户/Agent  ── NL ──> │ jijiang-text-to-pluto (workspace-a6/skills)                                    │
                     │   scripts/main.py: spec / preview / run / status                              │
                     │   scripts/validate.py: case / text / spec_file / confirm / target            │
                     └───────────────┬───────────────────────────────────────────────────────────────┘
                                     │ 本机回环（固定 argv，不拼接用户文本）
                                     ▼
                     http://127.0.0.1:8765/run  （Windows: bridge.py → wsl.exe -d Ubuntu）
                                     │
                                     ▼
                     ┌──────────────────── WSL Ubuntu ────────────────────┐
                     │ F:\FusionAgentHub\pluto（引擎，/mnt/f/...）        │
                     │  t2pluto.py      NL 解析 / 单位归一 / ini 渲染回读 │
                     │  pluto_runner.py 运行 / 指标提取 / 验收门 / 结果   │
                     └───────┬───────────────────────────┬────────────────┘
                             │                           │
                  复制模板二进制/ini                pluto_vtk_extract.py
                             ▼                           ▼
        F:\FusionWorkspace\workspace\<case>\pluto\runs\<run_id>\   ← 独立算例目录
        F:\PLUTO\**（只读模板与已编译二进制）
```

关键设计取舍：

- **引擎在 Windows 侧仓库、在 WSL 执行**。Gateway 的 distro 无 `/mnt/f` 挂载（automount disabled），因此技能不直接读写 F 盘，而是经回环桥把**固定命令**转发到 Ubuntu。
- **用户文本永不进入 shell**。桥只接收固定 argv（引擎路径、spec 路径、case、确认位），自然语言先落成 JSON spec，再由引擎在白名单内注入。
- **结果与模板解耦**。每次运行拥有独立目录，模板目录不被写入（曾发生过直接跑模板目录覆盖 data 的事故，已通过复制策略杜绝）。

## 3. 受控 Spec 契约

`pluto/templates.json` 是唯一真源。spec 的 schema 固定为：

```json
{
  "schema_version": "1.0",
  "case": "a6-demo",
  "template": "icf-multilayer-1d",
  "params": {"R_INNER_DT": 0.015, "R_GOLD": 0.0155},
  "grid": {"n1": 200, "x1max": 0.1},
  "time": {"tstop": 5e-13, "cfl": 0.4, "first_dt": 5e-14},
  "solver": {"hydro": "hllc"},
  "boundary": {"x1beg": "reflective", "x1end": "outflow"},
  "output": {"vtk_interval": 5e-13, "mu_factor": 1.0},
  "target": {"max_rho": [15, 20], "max_logp": [9.5, 10.5]},
  "trace": {"explicit": [], "defaults": [], "unassigned_numbers": []}
}
```

- 所有物理量统一为 **CGS**：cm / g·cm⁻³ / s / K / dyn·cm⁻²。
- 单位换算覆盖长度 mm/µm/nm、密度 kg/m³、时间 ns/ps/fs、温度 eV/keV、压力 Pa/GPa/bar。
- `trace.explicit` 记录每个参数的原值与单位，`trace.defaults` 记录补全项，`unassigned_numbers` 记录未能归属的数字，便于审计。
- `constraints` 在 spec 归一化后强制校验：半径递增、`x1max ≥ R_BERYLLIUM`、`T_RAMP0 < T_RAMP1`、`T_FOOT ≤ T_PEAK` 等。

### 3.1 `icf-multilayer-1d`（HD，1D 球形）

二进制 `scan/runs/run_60/pluto`，ini `scan/runs/run_60/pluto.ini`。

| 参数 | 单位 | 范围 | 含义 |
|---|---|---|---|
| `R_INNER_DT` | length | 0.002–0.09 | DT 燃料内半径 |
| `R_GOLD` | length | 0.002–0.09 | 金层外半径 |
| `R_COPPER` | length | 0.002–0.09 | 铜层外半径 |
| `R_FUEL_OUTER` | length | 0.005–0.2 | 燃料层外半径 |
| `R_FOAM` | length | 0.005–0.2 | 泡沫层外半径 |
| `R_BERYLLIUM` | length | 0.005–0.2 | 铍层外半径 |
| `RHO_DT` | density | 1e-4–5.0 | DT 初始密度 |
| `TEMP_INIT` | temperature | 10–5000 | 初始温度 |
| `grid.n1` | — | 100–4096（默认 2000） | X1 网格点数 |
| `grid.x1max` | length | 0.02–1.0（默认 0.1） | 计算域外半径 |
| `time.tstop` | time | 1e-13–1e-6（默认 2e-7） | 终止时间 |
| `time.cfl` | — | 0.1–0.8 | CFL |
| `time.first_dt` | time | 1e-18–1e-9 | 初始步长 |
| `solver.hydro` | enum | hllc/hll/roe/tvdlf | 黎曼求解器 |
| `boundary.x1beg/x1end` | enum | reflective/outflow | 边界 |

锁定（不可被文本覆盖）：`R_OUTER=0.1`、`RHO_GOLD=19.32`、`RHO_COPPER=8.96`、`RHO_FOAM=0.01`、`RHO_BERYLLIUM=1.85`、`RHO_FOAM_OUTER=0.1`、`GAMMA=1.6666667`。

### 3.2 `icf-rad1d-grey`（HD + RADIATION 灰体 FLD）

二进制 `T006_rad_1D/pluto`，ini `T006_rad_1D/pluto.ini`。

| 参数 | 单位 | 范围 | 含义 |
|---|---|---|---|
| `R_DT` | length | 0.05–0.3 | DT 燃料半径 |
| `R_OUT` | length | 0.06–0.4 | 烧蚀层外半径 |
| `RHO_DT` | density | 1e-5–0.1 | DT 密度 |
| `RHO_AL` | density | 0.1–10.0 | 铝烧蚀层密度 |
| `RHO_FILL` | density | 1e-9–1e-2 | 外部填充密度 |
| `T_INIT` | temperature | 10–5000 | 初始温度 |
| `T_FOOT` | temperature | 1e4–1e7 | 驱动 foot 温度（约 80 eV） |
| `T_PEAK` | temperature | 1e4–1e7 | 驱动峰值温度（约 255 eV） |
| `T_RAMP0` | time | 0–1e-7（默认 8e-9） | 脉冲爬升起点 |
| `T_RAMP1` | time | 0–1e-7（默认 12e-9） | 到达峰值时间 |
| `KAPPA0` | — | 0.1–1e4 | 灰体吸收系数 |
| `P_FILL` | pressure | 1e6–1e10 | 填充压力下限 |
| `GAMMA_EOS` | — | 1.2–2.0 | 绝热指数 |
| `grid.n1` | — | 64–2048（默认 320） | 网格点数 |
| `grid.x1max` | length | 0.05–2.0（默认 0.5） | 计算域外半径 |

锁定：`CONST_RAD=7.5657e-15`、`CONST_IDEALGAS=3.247353586e-7`、`REDUCED_C=1.0e9`、`RAD_C=2.99792458e10`、`FLUX_FRAC=0.25`。

## 4. 处理流程

### 4.1 自然语言解析

`t2pluto.parse_text()`：

1. 去 BOM、统一全角/半角空白；
2. **显式赋值优先**：`R_GOLD=0.155 mm` / `tstop=0.5 ps`；
3. **角色关键词最近匹配**：先按最长别名匹配（“DT燃料内半径”优先于“内半径”），避免“燃料层外半径”被“燃料半径”抢占；
4. **前/后子句归属**：数值可写在角色前（“0.15 mm 的 DT 燃料内半径”）或后（“DT 燃料内半径 0.15 mm”）；
5. **时间曲线特例**：`_RAMP_ALIASES` 把“8 ns 开始爬升”“12 ns 到达峰值”映射到 `T_RAMP0/T_RAMP1`；
6. 单位量纲校验：长度参数拒绝 `K`，时间参数拒绝 `mm`；
7. 模板识别：按 `text_keywords` 打分，辐射类关键词优先；
8. 默认值补全并记录来源，约束校验后生成 `schema_version=1.0` 的 spec。

### 4.2 ini 渲染与回读

`render_ini()`：

- 只改 `[Grid]`、`[Time]`、`[Solver]`、`[Boundary]`、`[Static Grid Output]`、`[Parameters]` 六段；
- `X1-grid` 强制写为 `1 0.0 <n1> u <x1max>`（球形网格）；
- 输出控制强制 `dbl=-1`、`flt=-1`、`vtk=tstop`，保证只产生**一个终态 vtk**；
- 其余段落与注释原样保留，输出断言纯 ASCII。

`verify_rendered_ini()`：回读 ini，逐项比对参数、`X1-grid`、`tstop/cfl/first_dt`、vtk 间隔，相对误差 ≤ `1e-9` 才继续；同时校验未知参数、缺失参数、网格点数。

### 4.3 执行与指标

`pluto_runner.run_case()`：

1. 生成 `run_id = <UTC时间戳>-<fingerprint>`（fingerprint 为 spec 的 SHA256 前 12 位），创建 `runs/<run_id>/`；
2. 复制模板二进制、写 `pluto.ini`、`pluto_spec.json`；
3. 执行：POSIX 下 `./pluto -i pluto.ini`；Windows 下固定 `wsl.exe -d Ubuntu --cd <run_dir> -- ./pluto -i pluto.ini`；
4. 查找终态 `data.*.vtk`，调用 `F:\FusionWorkspace\scripts\pluto_vtk_extract.py` 提取 `max_rho`、`max_p_over_rho`、`max_logp`、`max_T_K`；
5. 写 `result.json` 与 `latest.json`。

### 4.4 验收门

| 门 | 判定 |
|---|---|
| `solver_rc` | rc=0 且未超时 |
| `param_injection` | ini 回读与 spec 逐项一致（≤1e-9），参数计数与 `n1` 正确 |
| `final_vtk` | 存在终态 `data.*.vtk` |
| `metrics_finite` | `max_rho > 0` 且 `max_logp` 有限 |
| `target` | 可选；区间目标逐项落在 `[lo, hi]`，单点目标按容差 |

全部 pass 时 `ok=true`；失败保留 `run.log`、`pluto.ini` 供复现，**不自动重试**。

## 5. 代码与接口

| 文件 | 职责 |
|---|---|
| [templates.json](file:///f:/FusionAgentHub/pluto/templates.json) | 模板注册表：二进制/ini、范围、白名单、别名、锁定参数、约束 |
| [t2pluto.py](file:///f:/FusionAgentHub/pluto/t2pluto.py) | 单位归一、NL 解析、模板识别、约束校验、spec 指纹、ini 渲染与回读 |
| [pluto_runner.py](file:///f:/FusionAgentHub/pluto/pluto_runner.py) | spec 落盘、求解执行、vtk 提取、验收门、结果 JSON、CLI |
| [test_t2pluto.py](file:///f:/FusionAgentHub/pluto/test_t2pluto.py) | 单元测试与 `--smoke` 真实短算例 |
| [SKILL.md](file:///f:/FusionAgentHub/openclaw-skills/jijiang-text-to-pluto/SKILL.md) | OpenClaw 技能声明（Anatomy） |
| [main.py](file:///f:/FusionAgentHub/openclaw-skills/jijiang-text-to-pluto/scripts/main.py) | 技能编排层：校验 → 桥/子进程调用 → 结果整形 |
| [validate.py](file:///f:/FusionAgentHub/openclaw-skills/jijiang-text-to-pluto/scripts/validate.py) | case/text/spec 路径/confirm/target/spec 形状校验 |
| [test_anatomy.py](file:///f:/FusionAgentHub/openclaw-skills/jijiang-text-to-pluto/scripts/test_anatomy.py) | 离线解剖测试（不启动求解器） |
| [assets/config.yaml](file:///f:/FusionAgentHub/openclaw-skills/jijiang-text-to-pluto/assets/config.yaml) | 桥地址、引擎路径、模板根、超时 |
| [assets/schemas/pluto_spec.schema.json](file:///f:/FusionAgentHub/openclaw-skills/jijiang-text-to-pluto/assets/schemas/pluto_spec.schema.json) | spec JSON Schema（数值或带单位字符串） |
| [assets/prompts/llm_spec_prompt.txt](file:///f:/FusionAgentHub/openclaw-skills/jijiang-text-to-pluto/assets/prompts/llm_spec_prompt.txt) | LLM 抽取提示词（含硬约束与示例） |
| [assets/fixtures/test_specs.json](file:///f:/FusionAgentHub/openclaw-skills/jijiang-text-to-pluto/assets/fixtures/test_specs.json) | 合法/非法 spec 夹具 |

引擎 CLI：

```bash
python3 /mnt/f/FusionAgentHub/pluto/pluto_runner.py templates
python3 /mnt/f/FusionAgentHub/pluto/pluto_runner.py parse   --text "..." --case demo
python3 /mnt/f/FusionAgentHub/pluto/pluto_runner.py run     --spec-file <spec.json> --case demo [--confirm] [--target-file t.json] [--summary]
python3 /mnt/f/FusionAgentHub/pluto/pluto_runner.py extract --vtk data.0007.vtk
python3 /mnt/f/FusionAgentHub/pluto/pluto_runner.py status  --case demo
```

技能 CLI：

```bash
python3 main.py templates
python3 main.py spec    --text "..." --case demo
python3 main.py preview --spec-file /mnt/f/FusionWorkspace/workspace/demo/pluto/specs/xxx.json --case demo
python3 main.py run     --spec-file ... --case demo --confirm --timeout 600
python3 main.py status  --case demo
```

`main.py` 的调用策略：

- Ubuntu 本地存在 `/mnt/f/FusionAgentHub/pluto/pluto_runner.py` → 直接 subprocess 调用（开发/测试路径）；
- 否则（Gateway distro 无 `/mnt/f`）→ 经 `http://127.0.0.1:8765/run` 转发**固定 argv**；
- 桥 stdout 有 8000 字符截断，因此 run 固定携带 `--summary`，避免 plc 日志淹没 JSON。

## 6. A6 Agent 接入

| 位置 | 变更 |
|---|---|
| [agents.yaml](file:///f:/FusionAgentHub/agents/agents.yaml#L116-L127) | A6 增加 `openclaw_skill: 'jijiang-text-to-pluto'` |
| [A6-radhydro-pluto-castro.md](file:///f:/FusionAgentHub/agents/A6-radhydro-pluto-castro.md#L34-L57) | frontmatter 增加 `openclaw_skill`；新增“自然语言单算例入口”章节（流程、硬边界、验收门、与五步链路关系） |
| [a6-AGENTS.md](file:///f:/FusionAgentHub/openclaw-skills/workspace-guidance/a6-AGENTS.md) | 合并 A6 工作区指引并追加 text-to-PLUTO 入口，已部署为 Gateway `workspace-a6/AGENTS.md` |
| [README.md](file:///f:/FusionAgentHub/openclaw-skills/README.md#L5-L14) | 技能表加入 `jijiang-text-to-pluto`，说明桥固定 argv 例外 |

与五步链路的关系：

```
text-to-PLUTO（单点）  ──定标/闭环──>  LHS 采样 → 批量扫描 → 代理建模 → 帕累托 → 闭环验证
        ▲                                                                    │
        └──────────────── 帕累托代表解落到真实求解器 ◄────────────────────────┘
```

## 7. 部署

| 位置 | 内容 |
|---|---|
| `F:\FusionAgentHub\pluto` | 引擎（规范源） |
| `F:\FusionAgentHub\openclaw-skills\jijiang-text-to-pluto` | 技能（规范源） |
| `E:\OpenClaw\skills\jijiang-text-to-pluto` | 宿主部署副本 |
| `/home/openclaw/.openclaw/workspace-a6/skills/jijiang-text-to-pluto` | Gateway A6 实际加载目录 |
| `F:\FusionWorkspace\workspace\<case>\pluto\` | 运行产物 |

Gateway 部署注意事项：

- Gateway distro（`OpenClawGateway`）未挂载 `/mnt/f`，且不能以 root 修改 automount；文件部署采用 **base64 + tar 经 stdin** 写入，避免依赖 `/mnt/f` 或 `\\wsl$`。
- 部署后删除 `__pycache__`，并运行 `python3 .../scripts/test_anatomy.py` 确认 `ANATOMY_TEST=pass`。
- 执行前需在 Windows 启动 `python bridge.py`（监听 `127.0.0.1:8765`），并用 `curl http://127.0.0.1:8765/health` 验证。

## 8. 实测结果（2026-10-22）

### 8.1 单元测试

```text
T2PLUTO_TEST=pass (0 failures)
```

覆盖：单位换算、两套中文算例解析、两种模板渲染与回读、8 类非法输入拒绝、vtk 提取。

### 8.2 真实短算例（`icf-multilayer-1d`，200 网格，0.5 ps）

| 项 | 值 |
|---|---|
| run_id | `20261022T110804-b125d0b451b5` |
| solver rc | 0 |
| 终态文件 | `data.0007.vtk` |
| `max_rho` | 19.320009231567383 |
| `max_logp` | 9.998952226859657 |
| `param_injection` | pass（15 项、n1=200） |
| `target` | pass（max_rho ∈ [15,20]，max_logp ∈ [9.5,10.5]） |

结果目录：[result.json](file:///f:/FusionWorkspace/workspace/a6-demo/pluto/runs/20261022T110804-b125d0b451b5/result.json)。

### 8.3 技能端到端

- `spec`：生成 `specs/spec-20261022T110652865759.json`，`schema_version=1.0`，`trace` 完整；
- `run`：`ok=true`，五项验收门全 pass；
- 解剖测试：`ANATOMY_TEST=pass`（SKILL.md 声明、anatomy 字段、case/text 校验、路径穿越/跨 case 拒绝、确认门、target、夹具、JSON 提取）。

## 9. 安全与可审计性

| 风险 | 处置 |
|---|---|
| 用户文本注入 shell | 文本只进 JSON spec；桥只接收固定 argv |
| 任意 C/definitions.h 修改 | 不提供该路径；只改 ini 白名单 |
| 路径穿越 | `spec_file` 必须位于 `/mnt/f/FusionWorkspace/workspace/<case>/pluto/`，拒绝 `..` 与跨 case |
| 未确认执行 | `confirm=true` 为硬前置；否则仅 dry-run |
| 大文件/长任务 | `timeout` 10–3600 s，默认 600 s；rc 与超时分别判定 |
| 模板被污染 | 只读 `F:\PLUTO`；每次复制到独立 run 目录 |
| 温度误用 | `max_T_calibrated=false` 随结果返回，禁止热力学结论 |

## 10. 已知局限

1. **不重新编译**：无法改 `definitions.h`，因此不能切换 EOS、开启/关闭辐射、改坐标系统；这些需扩展模板并重新编译后注册。
2. **仅两个模板**：Z-pinch、喷射、多群辐射等尚无模板。
3. **`max_T_K` 未标定**：由 `mu_factor` 推出的温度量级不可信（~1e12 K），仅作诊断字段。
4. **单算例串行**：不做批量并发；批量仍由 A6 五步链承担。
5. **自然语言覆盖有限**：别名表与中文角色词为主，复杂长句可能落入 `unassigned_numbers`，需用户改用显式 `KEY=value`。
6. **Gateway 执行依赖桥**：桥未启动时只能做离线 spec/preview。

## 11. 后续演进

- [ ] 增加 Z-pinch / 喷射模板（含对应编译二进制与 `definitions.h` 版本指纹）
- [ ] `max_T_K` 标定：用已知解析解或 Castro 对标确定 `mu_factor`
- [ ] 多群辐射模板（替代灰体 FLD）
- [ ] 把 text-to-PLUTO 结果自动回注 A6 数据集（主动学习闭环）
- [ ] 桥侧增加结构化 argv 端点，彻底移除命令字符串路径
