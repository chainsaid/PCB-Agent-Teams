# 工具箱参考 — 参数 + 输出 schema

**目录**:`init_pcb` · `placement_brief` · `init_layout` · `get_geometry` · `move` ·
`check_placement` · `render` · `refit_board` · `bridge_slot` · `add_zones` · `check_zones` ·
`stitch_zones` · `set_silk_spec` · `net_metrics` · `set_design_rules` ·
`run_drc` · `route` —— Phase D/E 用到的 `refit_board` / `bridge_slot` / `add_zones` /
`check_zones` / `stitch_zones` / `set_silk_spec` / `net_metrics` / `set_design_rules` /
`run_drc` / `route` 都在文件后半段,别只读前 100 行。

所有工具在 `scripts/tools/`,用工作区 `.venv` 的 python 跑,**JSON 打到 stdout**。
失败一律 `{"ok": false, "error": "..."}`。

⚠️ **两个例外**:`init_pcb` / `init_layout` 的实现在 `scripts/` 而非 `tools/`
(`scripts/sch_to_pcb.py` / `scripts/place_components.py`),且它们的 stdout 是
**人读文本 + `--- JSON ---` 分隔线之后才是 JSON** —— 直接 `json.loads(stdout)` 会炸,
先按分隔线切。其余工具 stdout 是纯 JSON。

文件清单(路径即调用目标):
`scripts/tools/placement_brief.py` `scripts/tools/get_geometry.py` `scripts/tools/move.py`
`scripts/tools/check_placement.py` `scripts/tools/check_chirality.py`
`scripts/tools/render.py` `scripts/tools/refit_board.py`
`scripts/tools/bridge_slot.py` `scripts/tools/add_zones.py` `scripts/tools/check_zones.py`
`scripts/tools/stitch_zones.py` `scripts/tools/set_silk_spec.py`
`scripts/tools/net_metrics.py` `scripts/tools/set_design_rules.py`
`scripts/tools/run_drc.py` `scripts/tools/route.py` `scripts/tools/_kicad.py`
`scripts/sch_to_pcb.py` `scripts/place_components.py` `scripts/drc_exclusions.py`

```bash
PY=".venv/bin/python"     # 工作区根的 venv,相对路径——别写死绝对路径,换机就废
T=".claude/skills/draw-pcb/scripts/tools"
```

## init_pcb — sch → 空 .kicad_pcb

底层 `scripts/sch_to_pcb.py`(非 tools/ 下)。

```bash
"$PY" .claude/skills/draw-pcb/scripts/sch_to_pcb.py <dir-containing-.kicad_sch>
```
输出:`pcb_path` / `footprints_added` / `nets_added`。已存在则 draw-pcb 回路里跳过。

## placement_brief — 电路事实

```bash
"$PY" $T/placement_brief.py <pcb>
```
输出字段:
- `domains` — `{HV:[refs], ISO:[...], LV:[...]}`
- `footprint_domain` — `{ref: domain}`
- `barrier_x` — 隔离屏障 x(隔离器件中心均值)。**仅元件摆好后有效**,空板为 null;Phase A/B 的 `--barrier-x` 取 `init_layout` 输出的 `slots[].x_mm`
- `barrier_devices[]` — `{ref, value, bridges_grounds[], pads:[{number,net}], note}`(跨 ≥2 地网的隔离器件;旋转推理就靠 `pads`)
- `edge_devices[]` — J* 连接器 + SW* 开关,必须贴板边
- `cap_ic_links[]` — `{cap, ic, via_net, gnd}`(cap 该贴哪个 IC,机械事实)
- `chains[]` — `{members:[有序], domain, kind}`(串联链,members 已按串联顺序)
- `net_pads` — `{net: [ref.pad]}`
- `power_nets` / `ground_nets`

## init_layout — 确定性区域种子

底层 `scripts/place_components.py`。

```bash
"$PY" .claude/skills/draw-pcb/scripts/place_components.py <pcb> --claude-md <project>/CLAUDE.md
```
读项目 CLAUDE.md 的 `placement` 段,出区域分区 + 板框 + 隔离槽。当回路起点,不是终点。

## get_geometry — 每件几何

```bash
"$PY" $T/get_geometry.py <pcb> [--refs R1,U1] [--no-pads]
```
输出 `footprints[]`,每件:`ref / value / x,y / center[cx,cy] / angle / layer / type /
courtyard{min_x,min_y,max_x,max_y,w,h} / pads[{number,x,y,w,h,net}] / nets[]`。
`board` = Edge.Cuts bbox(没有则 null)。**`center` 就是 `move` 的目标点。**

## move — 移动 / 旋转

```bash
"$PY" $T/move.py <pcb> --move "R1:42,18,90" --move "C8:50,20" ...
"$PY" $T/move.py <pcb> --moves-json moves.json     # {"R1":[42,18,90],...}
```
target (x,y) = body-bbox 中心。rot 可省(保持原旋转)。输出 `moved[] / not_found[]`。
`-o/--out` 写到另一个 `.kicad_pcb`(不给就原地改);keep-best 存每轮快照就用它。
只动 footprint 位置,不碰 Edge.Cuts / 走线 / zone——可在回路里反复调。
⚠️ 但板上**已经有铜 / 桥**时,move 完那两样就 stale 了:必须重跑
`refit_board → bridge_slot → add_zones → check_zones → run_drc`,否则桥停在旧位置、铜绕的是旧 courtyard。

## check_chirality — 封装手性(镜像)闸门(Phase A,拿到板 / 库先跑)

```bash
"$PY" $T/check_chirality.py <board.kicad_pcb | dir.pretty>
```
镜像封装是 **DRC 盲区**:y 翻转保距离,间距 / 网长 / 布通率 / 覆铜全部照常绿,
错误要到贴片才暴露(非对称件对不上焊盘)。来源:从库帧 y 朝上的 EDA
(立创专业版 / Altium / Eagle)导库漏翻 y,或自绘时照着底视图画。

判据(KiCad 官方库 15430 个封装标定):**双排蛇形编号 + 排内 pitch ≤2.8mm +
引脚 ≥6** 的封装(SOIC / SOP / DIP / ESOP 类几何签名,不靠名字),引脚环从
正面看必须逆时针(JEDEC 约定;B.Cu 相反)。顺时针 → 进 `cw_serpentine[]`。
pitch 上限排除变压器类(官方库 24 个合法顺时针全在 pitch ≥4mm);
名带 `Reverse` / `Clockwise` 的故意反向变体自动豁免进 `suppressed_variants[]`。

**`hard_fail` 是群体判据**:`cw_serpentine ≥2 且 > serpentine_ok` —— y 翻转是
系统性的,镜像库的蛇形件**全体同时反**,这正是它的签名。单件顺时针更可能是
厂家编号(显示模组 / 板对板连接器,官方库残留 ~1%),列名单不自动挡。

输出:`verdict`(`MIRRORED_LIBRARY` / `VERIFY_LISTED` / `CLEAN`)、`cw_serpentine[]`
(`{footprint, pins, signed_area, front}`)、`suspects[]`(非蛇形顺时针,参考)、
`serpentine_ok` / `ccw_ok` / `not_judgeable` 计数。**退出码** `0` / `2`(hard_fail)。

处置:`MIRRORED_LIBRARY` = 来源库整体镜像 → **修转换 / 修库重新生成,不要在板上
逐件改**;`VERIFY_LISTED` = 逐件对照 datasheet / 官方同款,确认是厂家编号才继续,
理由落在可见输出里。电阻电容 / 2 脚端子 x 对称,镜像了也看不出 —— 一个命中
就该怀疑整个来源库。

## check_placement — 合法性闸门

```bash
"$PY" $T/check_placement.py <pcb> [--min-clearance 0.2] [--barrier-x 31.8] \
      [--barrier-exempt R1,U2] [--decoupling-pairs C6:U1,C10:U1,C5:U3]
```
`--decoupling-pairs` 传项目 CLAUDE.md 声明的 cap:IC 对(权威配对);`cap_far_from_ic`
按它 + 真实 pad-to-pad 距离判,不传则退回 net 推断(可能配错 IC)。
输出:
- `hard_fail` — **闸门信号**,true = 有硬违例
- `score` — 0-100,合法性进度(非质量,别当目标函数)
- `metrics` — `{hpwl_mm, courtyard_overlaps, out_of_board, pad_clearance_violations, barrier_crossings}`
- `violations[]` — `{type, severity, refs, detail}`(硬违例)
- `warnings[]` — `{type, refs, detail}`:`connector_not_on_edge`(连接器没贴板边)、`geometry_uncertain`(courtyard 退化,extent 不可靠)。**非 hard_fail,但必须逐条处理。**

`--barrier-x` 给了会自动调 `placement_brief` 豁免真隔离器件;`--barrier-exempt` 手动补。

## render — 标注 PNG

```bash
"$PY" $T/render.py <pcb> -o out.png [--ratsnest] [--barrier-x 31.8] [--label-pads]
```
蓝=F.Cu 正面,绿=B.Cu 背面,红框=courtyard 重叠,橙虚线=隔离屏障,灰线=飞线(--ratsnest)。
输出 `png / overlap_count / overlaps[]`。**回路里必须 Read 这张图做视觉判断。**

## refit_board — 板框贴合布局(Phase D,最先跑)

```bash
"$PY" $T/refit_board.py <pcb> [--margin 2.5]
```
把 Edge.Cuts 外框 + 隔离槽缩到所有 footprint 的实际范围 + margin。板框是
`init_layout` 按 CLAUDE.md `pack_density` 一次性定死的,回路收紧元件后板框不会自己跟着缩——
refit 补这一步。隔离槽按检测到的 x 重画为连续槽(留 3mm 上下桥),之后再跑 `bridge_slot`。
**必须在 `bridge_slot` / `add_zones` 之前**(两者都读 Edge.Cuts)。
输出 `board{x,y,w,h} / slot_x_mm / fill_ratio`。`fill_ratio` = footprint bbox 总面积
(含 pad + 图元,不含文字)/ 板面积,紧凑度指标(阈值见 `loop.md` route-ready 验收第 7 项)。
**不是** courtyard 面积——多数封装库不带 CrtYd 图元,按 courtyard 算会退化成 pad 面积,
换个库同一块板的数就变了。

**`--keep-outline`**:板框由外部给定(外壳 / 机箱 / 客户规格)时用这档。**完全不动 Edge.Cuts**,
只按板上现有板框回报 `fill_ratio` + `placement_extent`,`wrote_nothing: true`,一个字节都不写。
这是锁框场景下拿 `fill_ratio` 的唯一正路——默认档会把板框删掉重画。

## bridge_slot — 隔离槽留桥(Phase D)

```bash
"$PY" $T/bridge_slot.py <pcb> [--margin 1.0]
```
重画隔离槽,在每个跨槽 barrier 器件(自动从 `placement_brief` 取)下方留实体桥。
**元件摆定 + refit_board 后才跑**。输出 `slot_x_mm / bridges[] / slot_segments_drawn`。

## add_zones — GND 铺铜(Phase D + Phase E 重铺)

底层 `_kicad_python_helper.py` 的 `add_ground_zones` mode。create+fill 幂等一步,
幂等粒度 `(net, layer)`。**铺哪面 / 哪个网 / 铺不铺由 AI 按 `copper_pour.md` 判断**,
工具只执行。

```bash
"$PY" $T/add_zones.py <pcb> [--layers B.Cu,F.Cu] [--nets LV_GND HV_GND] [--clearance 0.3]
                            [--rect XMIN YMIN XMAX YMAX]
                            [--pad-connect {thermal,solid,none}]
```

- `--layers`:逗号分隔铜层,默认 `B.Cu`。
- `--rect`:把铜限制在这个矩形内(mm),与按网/按槽算出的矩形取交集。zone 只有一个
  clearance,表达不了逐电压的爬电距离 —— 让铜**离开高压区**用这个,别去放大 `--clearance`。
- `--nets`:限定只铺名字含这些子串的 GND 网;不给则铺全部 GND-like 网。
  多 GND 网按域分开调用 —— 如 `--layers F.Cu --nets LV_GND` 只在正面铺低压地。
- `--pad-connect`:铜怎么接焊盘。`thermal`(默认)= 花焊盘辐条,手焊友好,但每根辐条都要地方,
  焊盘周围一挤就报 `starved_thermal`;`solid` = 实心连接,没有辐条可挨饿,代价是每个焊盘都变散热器
  (回流焊无妨,手工返修变难);`none` = 不连(极少用)。**这是工艺权衡,按装配方式选,别按 DRC 数字选**。
- Phase E 布线后在 `_routed` 板上**重跑一次**,铜绕开新走线 / 过孔。
- 两层板铺完铜**还要跑 `stitch_zones`** 把两面地缝起来——铺铜本身不打缝合孔,
  底面铜被走线切碎后回流路径是断的。

## check_zones — 隔离屏障铜跨槽校验(Phase D + Phase E 重铺后,两处都要跑)

底层 `_kicad_python_helper.py` 的 `validate_zones` mode。只读,不存板。

```bash
"$PY" $T/check_zones.py <pcb> [--tol 0.05]
```
断言**没有任何铜 zone 跨隔离屏障**:取每个已填充 zone 的填充铜 X 跨度,
若同一 zone 既有铜在槽左又有铜在槽右 → 跨屏障 → 退出码 1 + `crossings[]`。

> ⚠️ **退出码语义不统一,别用 `&&` 串闸门**:只有 `check_zones` 用退出码 1 表示 fail。
> `check_placement` **无论 `hard_fail` 真假都退出 0**(只有文件不存在 / 异常才 1)——
> 必须读 JSON 里的 `hard_fail` 字段判,靠 shell 退出码会把闸门当通过。
net/电压/网名无关,**任意单竖直隔离槽通用**。无槽 → 跳过(`slot_detected=False`,
不误报)。横向/多段屏障未覆盖。**铺完铜跑(DRC clearance 之外的专门防线)**。

## stitch_zones — 把一个网两面的铜缝起来(布线 + 铺铜之后)

```bash
"$PY" $T/stitch_zones.py <pcb> [--net GND] [--pitch 5.0] [--layers F.Cu B.Cu] \
      [--via-dia 0.6] [--drill 0.3] [--clearance 0.2] [--min-sep 2.0] \
      [--hole-to-hole 0.5] [--thermal-pad-vias MIN_AREA_MM2]
```
**为什么需要**:两层板上底面铜被每条 B.Cu 走线切碎,信号底下的回流路径要不停找地方过去
(EMC 报参考平面断裂),顶面的大散热焊盘也没有东西把热和回流带到另一面。两件事同一个缺口:
**在两面铜都真填到的地方打过孔**。

判据:候选点必须同时落在**两面填充多边形各自内缩(过孔半径 + 间距)之后**的区域内,
这样过孔永远碰不到异网铜(填充区本身已经和非本网的一切保持了距离)。
⚠️ **光判铜会漏**:金属化通孔焊盘就坐在铜里,只判铜的话工具会在它的钻孔正上方说"可以",
撞出 `hole_to_hole`。所以孔另立一条判据,按孔到孔间距单独查。

**一次只缝一个网,网名必须显式给**。自动匹配所有 GND-*ish* 网会顺手把隔离屏障两侧缝通,
把屏障存在的意义销毁——HV_GND / LV_GND 的板分两次跑。

`--thermal-pad-vias <mm²>` 额外在该网**贴片大焊盘内部**打散热孔(铺铜判据找不到它们——
焊盘不是铺铜)。输出 `stitch_vias_added / thermal_pad_vias_added / candidates_rejected{}`。
实测一块两层板:5mm 间距下加 113 + 2 个过孔,**DRC 违例数一条没涨**(`hole_to_hole` / `annular_width` 均 0)。

## set_silk_spec — 全板丝印字高 / 线宽统一

```bash
"$PY" $T/set_silk_spec.py <pcb> [--mil] [--height X] [--thickness X] \
      [--include-values | --hide-values]
```
fab 会给丝印最小线宽和字高(低于它印出来发虚或掉字),而 KiCad 的每个 footprint 字号
来自各自的封装库 —— 不管的话一块板上并存好几种字号。位号会被放到该件所在面的丝印层。
Value 默认不动(它多半是 MPN,印在板上没用还挤掉位号)。**Phase D 靠后跑**(摆位定稿之后)。

⚠️ **两个后果是预期的,不是缺陷**:① 动过文字的件从此与库不一致,DRC 会对每一件报
`lib_footprint_issues` —— 这类**按已知项分诊,不要去追**;② 字变大必然更容易压到东西,
`silk_overlap` / `silk_over_copper` 会涨 —— 这个是**真的可读性问题**,靠挪文字解决,不是靠放弃规格。

## net_metrics — 每网长度 / 过孔数 / 线宽(只读)

```bash
"$PY" $T/net_metrics.py <pcb> [--nets PAT ...] [--max-vias N] \
      [--max-length MM] [--min-width MM] [--pairs "A,B" ...]
```
布线约束的**量具**:过孔预算、长度上限、差分对等长,全靠它验。底层复用
check-pcb 的 `analyze_net_lengths`(同一实现,两边不会对不上)。
`--pairs "D+,D-"` 给出该对的 `length_mm` / `delta_mm` / `via_count`。
传了 `--max-vias` / `--max-length` / `--min-width` 时,越界的网进 `violations[]`
且**退出码 2**,可以直接串进闸门链。

> 📌 **「我给布线器传了约束」不是约束达成的证据**。等长参数会在通道装不下蛇形时静默不动,
> 过孔代价只是代价不是上限 —— 结论一律以本工具量出来的数为准。

## set_design_rules — 把 fab 规则写进 .kicad_pro(布线前跑)

```bash
"$PY" $T/set_design_rules.py <pcb> [--mil] \
      [--min-track-width X] [--min-clearance X] [--min-via-diameter X] \
      [--min-through-hole X] [--min-copper-edge-clearance X] \
      [--min-hole-clearance X] [--min-hole-to-hole X] [--min-annular-width X] \
      [--netclass NAME] [--track-width X] [--clearance X] \
      [--via-diameter X] [--via-drill X] \
      [--diff-pair-width X] [--diff-pair-gap X] [--nets PAT ...]
```
**为什么必须显式设**:`kicad-cli drc` 的规则来自 `.kicad_pro`,**不看** `.kicad_pcb` 的 setup 块。
项目文件还是默认规则时,DRC 是拿"这块板从没打算遵守的规则"在量它——报告看着权威、每个数都不对。
反方向同样坑:规则比 fab 松,板子这边过、厂里挂。

`--mil` 让所有数值按 mil 输入(fab spec 常用 mil,免得手算)。只改你传了的那几项,项目文件其余部分不动。

两层规则 KiCad 都会执行:全局下限(`design_settings.rules`)+ 每 netclass 值(`net_settings.classes`)。
**netclass 值低于全局下限会让 DRC 把整块板全标红** —— 工具检出这种矛盾就写进 `conflicts[]` 回报,
**不替你挑赢家**。`--netclass` 指的类不存在时按 Default 克隆新建(继承全部字段,不给 KiCad 留空档),
`--nets` 把网名 pattern 挂到该类(写 `netclass_patterns`)。

输出 `changed{改了什么: [旧值,新值]} / effective_mm{回读磁盘的生效值} / conflicts[]`。
**生效值是回读文件得来的,不是"我以为我写了什么"**。改完规则旧的 DRC 结果即作废,重跑 `run_drc`。

## run_drc — kicad-cli DRC(Phase D)

```bash
"$PY" $T/run_drc.py <pcb>
```
输出 `violation_count / unconnected_count / by_type`,缺同名工程文件时多一个 `warnings[]`。
⚠️ **会静默套用豁免**:它读项目 `.kicad_pro` 的 `drc_exclusions` 并把命中的违例从计数里
剔掉(`scripts/drc_exclusions.py`)。所以 `violation_count = 0` 的含义是"**没有未豁免的违例**",
不等于板子真干净。拿它当硬闸门前,先确认 `.kicad_pro` 里的豁免列表是你认可的。
⚠️ **板旁必须有同名 `.kicad_pro`**:`kicad-cli` 按板文件名找规则,找不到就**静默套用 KiCad 默认规则**。
实测同一块板:带工程文件 161 violations,不带 373——多出的 199 `drill_out_of_range` + 9 `annular_width`
+ 4 `copper_edge_clearance` 全是假的。工具现在检出缺失会在 `warnings[]` 里点名(不 fail,因为 DRC 本身跑成功了)。

## route — 自动布线(Phase E,可选)

```bash
"$PY" $T/route.py <placed-pcb> [--output X] [--in-place] [--keep-zones] \
      [--board-edge-clearance 0.6] [--nets PAT ...] \
      [--track-width MM] [--power-nets NET ... --power-nets-widths MM ...] \
      [--ordering {inside_out,mps,original}] [--via-size MM] [--via-drill MM] \
      [--clearance MM] [--layers LAYER ...] [--impedance OHM] \
      [--via-cost N] [--via-proximity-cost N] [--layer-costs MULT ...] \
      [--length-match-group PAT ...] [--length-match-tolerance MM] \
      [--meander-amplitude MM]
```
过孔预算(`--via-cost` / `--via-proximity-cost` / `--layer-costs`)与差分等长
(`--length-match-*` / `--meander-amplitude`)的判断框架见 `routing_strategy.md`——
**两者都要用 `net_metrics.py` 验结果**:代价不是上限,等长塞不进通道时会静默不动。
**退出码 1 = 有网没布通,但板文件已经写出来了**,别用 `&&` 串下一步(会静默跳过重铺 + DRC)。
产出还带 `sidecars_copied`:同名 `.kicad_pro` / `.kicad_dru` 会一起拷到 `_routed`,
否则 DRC 找不到规则就静默套默认值,凭空多出几百条假违例。
底层 vendored KiCadRoutingTools(KRT)。默认产出 `<stem>_routed.kicad_pcb`(不覆盖
placement 原件)。输出 `routed_single / multipoint_pads / failed / vias / recipe /
zones_stripped / output_pcb`。
**默认剥掉副本上的铺铜再布**(`zones_stripped` 回显剥了几个;keepout / rule area 不动)——
留着会让 KRT 判该网已连、整网跳过,重铺后碎成孤岛,原因 + 实测对照见 `routing.md`。
`--keep-zones` 是逃生门,用了就得自己查 `unconnected`。
**配方 flag(线宽 / 电源网 / 差分对 / ordering)是电路判断,先按 `routing_strategy.md`
给 net 分类再定**,未设的吃 KRT 默认。
**布完先 `add_zones` 重铺(routed 板上现在没有铜),再跑 `run_drc`** —— 顺序反了全是假违例。
需先 `build_router.py` 编译 KRT 的 Rust 模块。详见 `routing.md` + `routing_strategy.md`。
