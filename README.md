# ETEC306 — Edge–Cloud Collaborative Intelligent Security & Dynamic Target Tracking

**边缘–云协同智能安防与动态目标跟踪系统**

| Item | Detail |
| --- | --- |
| Course | ETEC 306 — Electrical and Computer Engineering Technology |
| College | Centennial College |
| Student | Tang Li (`Makabakamanihong`) |
| Repository | Capstone project record: version control, collaboration, and documentation |

## Overview

This repository tracks a headless Raspberry Pi edge node that uses millimeter-wave radar for spatial cueing, a servo pan/tilt camera for visual confirmation, and MQTT for cloud alerts. Everyday sensing stays on the edge; the cloud receives telemetry and intrusion events only when needed.

**Hardware stack:** Raspberry Pi 4B · HLK-LD2450 radar · PCA9685 + MG90S pan/tilt · SSD1306 OLED · USB camera · local keys / traffic-light status · Mosquitto (OCI) over TLS.

**Target metrics (course concept):**

- Track latency (radar frame → coarse mechanical align) ≤ 250 ms (software path reported separately; mechanical limit assessed for small-angle steps)
- PWM angular step theory < 0.5° (~0.44°/LSB at 50 Hz, 12-bit); measure servo deadband in lab
- Intrusion confirmation true-positive rate > 95% within ~5 m (with vision reject of non-human false alarms)

## Repository layout

```text
docs/technical-design.md   Full hardware/software technical draft
docs/project-plan.md       Phased checklist for the capstone
docs/collaboration.md      Git / Issues / Project board workflow
src/                       Edge software placeholders (radar, servo, vision, MQTT)
.github/workflows/         Lightweight CI checks
```

## Getting started

```bash
git clone https://github.com/Makabakamanihong/ETEC306.git
cd ETEC306
```

Branching: create a feature branch for each task, open a pull request into `main`, and keep commit messages short and intentional.

## Project management on GitHub

- **Issues** track hardware bring-up, radar tracking, vision, MQTT/alerts, and documentation.
- **Project board** columns: **To Do** · **In Progress** · **Done**.
- Prefer linking commits and PRs to issue numbers (e.g. `Closes #2`).

## Documentation map

1. Read this README for scope and workflow.
2. Read `docs/technical-design.md` for architecture, pins, power, and algorithms.
3. Use `docs/project-plan.md` as the week-by-week checklist.
4. Follow `docs/collaboration.md` for GitHub usage expected by the ETEC306 workbook.

## License / academic use

Course work for Centennial College ETEC 306. Update this section if the team later chooses an open-source license.
