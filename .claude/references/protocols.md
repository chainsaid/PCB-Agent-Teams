# 工作区元协议（极简版，迁自 CLAUDE.md）

> LLM 自己懂的东西不重复展开。这里只写跨 skill 共用的非平凡约束。

## USER.md / 项目 CLAUDE.md 自动维护

用户透露**长期事实**（地点 / 在手设备 / 焊接能力 / 长期方向 / 沟通偏好）→ 直接写对应文件并告知用户：USER.md（user 事实）/ 工作区 CLAUDE.md（工程铁律——但工作区 CLAUDE.md 已无 domain 内容，新铁律应归 SKILL.md）/ 项目 CLAUDE.md（电路细节）/ 自动 memory（跨工作区偏好）。绝对日期化（"周四"→"2026-05-09"）。

## 计划先行（非平凡 Projects/<name> 任务）

新建项目 / 选品 / longlist / BOM gate / 画 sch / DRC / EMC / fab / 跨文件修复 之前先写短计划：目标 + 边界 + 输入文件 + 阶段 + gate / 停机条件 + agent 分工。计划发给用户，按计划推进，发现计划外问题先调整再继续。**不允许为让流程继续而静默 workaround**。

## Sub-agent 分工 + 上下文预算

**默认拆给 sub-agent**：component-selecting longlist（按锚点拆）、bom-readiness / kicad / spice / emc 审查（agent 读 JSON 返回问题清单）、datasheet 清理审计（先报告再确认）、monitor 旁路日志。
**主线程独占**：最终 MPN / BOM 冻结、价格取舍、安规等级、HV/LV 隔离策略、删用户文件、修共享库 `lib_external/`、脚本失败后的替代路线。
**输出契约**：sub-agent 必须返回结构化摘要（结论 + 关键证据 + 失败原因 + 建议动作 + 改过哪些文件），不要把完整网页 / JSON 粘回来。

## 项目执行监控

涉及 `Projects/<name>/` 的非平凡任务，主 LLM 启动旁路 monitor sub-agent，写 `Projects/<name>/docs/skill_execution_log.md`（input → command → output → issues → result）+ `skill_execution_report.md`（阶段 complete/partial/fail）。监控不替代 BOM/ERC/DRC gate 结论。

## Phase 编号约定

工作区 Phase = 0–5 总流程（含 2.5 / 3.5 / 4.5 半阶，见根 CLAUDE.md 阶段表）；skill 内部 Phase / Stage 仅 skill 内有效。引用时写清楚是哪一种。
