# text-to-PLUTO

**Natural language → controlled PLUTO case.** A bounded OpenClaw skill that compiles a Chinese/English case description into a reproducible [PLUTO 4.4](http://plutocode.ph.unito.it/) run for radiation-hydrodynamics (ICF capsule) studies.

```
中文算例描述 → 受控 spec (JSON) → pluto.ini → 已编译 PLUTO → 终态指标 → 验收门 → result.json
```

Part of the FusionAgentHub A6 RadHydro agent toolchain.

## What it does

- Parses a natural-language case description (Chinese role keywords, aliases, explicit `KEY=value` overrides)
- Normalizes all quantities to CGS (mm/µm/nm, kg/m³, ns/ps/fs, eV/keV, Pa/GPa/bar …)
- Renders `pluto.ini` on a **registered template** and verifies the result by re-reading (≤1e-9)
- Runs the pre-compiled PLUTO binary in an isolated per-run directory
- Extracts `max_rho` / `max_p_over_rho` / `max_logp` from the final VTK
- Enforces acceptance gates: solver rc, parameter injection, final vtk, finite metrics, optional target intervals

## Hard boundaries (by design)

- Two registered templates only:
  - `icf-multilayer-1d` — 1D spherical multilayer capsule, HD
  - `icf-rad1d-grey` — 1D spherical grey-FLD radiation-driven implosion, HD+RADIATION
- Whitelisted parameters only; material constants locked; **no arbitrary C / `definitions.h`, no recompilation**
- Template directories are read-only; every run gets its own directory
- `run` requires explicit `confirm=true` (otherwise dry-run preview only)
- `max_T_K` is **not calibrated** (`max_T_calibrated=false`) — only `max_rho` / `max_logp` may support conclusions

## Layout

```
skill/    OpenClaw skill package (SKILL.md, scripts/, assets/)
engine/   NL parsing, unit normalization, ini render/verify, runner, tests
IMPLEMENTATION.md   full design doc (架构 / spec 契约 / 验收门 / 部署 / 实测)
```

## Quick start (engine CLI)

```bash
python3 engine/pluto_runner.py templates
python3 engine/pluto_runner.py run \
  --text "1D 球形多层靶丸：DT 燃料内半径 0.15 mm，金层外半径 0.155 mm，... 网格 200，计算域 1 mm" \
  --case demo --confirm --summary
```

Skill entry points: `spec` → `preview` → `run --confirm` → `status` (see `skill/SKILL.md`).

## Tests

```bash
python3 engine/test_t2pluto.py            # T2PLUTO_TEST=pass
python3 skill/scripts/test_anatomy.py     # ANATOMY_TEST=pass (offline, no solver)
```

Verified on PLUTO 4.4 / WSL Ubuntu. Reference run: solver rc=0, `max_rho≈19.32`, `max_logp≈9.999`, all gates pass.
