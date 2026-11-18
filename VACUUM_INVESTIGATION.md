# Vacuum 成功后版本数仍然偏高的调查

调查对象：LaminaDB（`/workspace`，基线 `cb08bd0`）
调查方式：仅通过公开 HTTP 接口在干净实例上复现；不修改产品代码。
可执行材料：`examples/vacuum_diagnose.py`（12 个自校验场景）。

## 一、结论摘要

`POST /vacuum` 的 HTTP 200 只表示“一次回收记录已耐久发布”，不表示“用户要求的空间已释放”。
在本基线里，一次成功调用之后 `stats.versions` 仍然偏高，来源可以分成四类，且彼此叠加：

| 类别 | 机制 | 是否应“修复产品” |
| --- | --- | --- |
| A. 活动快照钉住水位 | 有效水位 = min(请求值, 所有活动事务的 `snapshot`) | 否，这是快照隔离的承诺 |
| B. 请求水位本身陈旧 | 请求值低于 head，水位之后的写入按设计保留 | 否，调用方口径问题 |
| C. 墓碑与“每键至少一版”地板 | 墓碑链计入 keys/versions；水位前的不可见历史仍占位 | 否，回收规则本身 |
| D. 统计口径 | `versions` 统计版本条数，不等于可见行；`removed + retained` 是版本数而非空间 | 否，但需要正确解读 |

真正值得处理的是**可操作性**问题，而不是保护行为本身：
最小的那个快照决定一切；它关闭之前，任何 vacuum 都无法回收其后的版本。
并且只要关系上存在索引，**每次成功调用（即使 `removed=0`）仍会做一次全量索引重建并占用全局写锁**。

## 二、复现（干净实例，逐步区分）

所有场景都从空目录启动实例。以下数值来自本机实测（Go 1.25.4，linux/arm64）。

### 2.1 正常基线：没有读者时一次回收到底

种子 6 行 + 1 个索引，做 2 次同键更新 + 1 次删除（head=6）：

```
before:  versions=9  keys=6  index_entries=8
GET /vacuum {horizon:6} -> {"horizon":6,"removed":4,"retained":5,"pinned":false}
after:   versions=5  keys=5  index_entries=5
```

`versions == keys == 5` 是回收地板：每个存活键保留 1 个可见版本。`removed + retained = 9` 等于回收前版本数。
冻结图像（`GET /backup`）逐链审计满足保留规则；`at < horizon` 返回 410，`at == horizon` 可读。

### 2.2 一个老读者：成功、`pinned=true`、`removed=0`

5 行种子（seq 2）后开一个只读事务（snapshot=2），再做 20 轮 × 5 键更新（head=102）：

```
before:  versions=105  active=[{snapshot:2}]
POST /vacuum {horizon:102} -> {"horizon":2,"removed":0,"retained":105,"pinned":true}
after:   versions=105  sequence=103
```

- 有效水位被压到 2，102 次写入全部位于水位之后，**按规则一条都不能删**。
- 老读者仍读到旧值（`sample=1 → amplitude=10`），当前读读到新值（`1191`）。这是正确行为。
- 在钉住期间继续写入一个 5 行事务，版本数从 105 涨到 110——**写入越多，可回收的积压越大**。
- 再次 vacuum 仍为 `removed=0`；关掉读者后一次 vacuum `removed=105`，回到 5 版。
- 注意 `sequence` 每次 vacuum 都 +1：**空转的维护调用也在消耗序号和写日志**。

### 2.3 多个年龄不同的事务：只有最小快照说了算

同时开 snapshot 2 / 4 / 6 三个只读事务，再各做两轮更新（head=6）：

```
POST /vacuum {horizon:6} -> {"horizon":2,"removed":0,"retained":9,"pinned":true}
关闭 snapshot 4 -> {"horizon":2,"removed":0,...}   不变
关闭 snapshot 6 -> {"horizon":2,"removed":0,...}   不变
关闭 snapshot 2 -> {"horizon":6,"removed":4,"retained":5,"pinned":false}
```

关闭非最小快照的读者**没有任何回收效果**。这是 `maintenance.go:82-87` 对全部活动事务取最小 `Snapshot` 的直接结果。
把“结束一个合法长读”当作处置手段时，必须确认结束的正是最小快照者。

### 2.4 被钉住时仍可部分回收

种子 4 行，更新 key1（seq 3），再开读者（snapshot=4），之后更新 key1 两次、key2 两次（head=7）：

```
before:  versions=9
POST /vacuum {horizon:7} -> {"horizon":4,"removed":1,"retained":8,"pinned":true}
after:   versions=8
reader 仍能看到 snapshot 4 的完整 4 行，且 key1 的值是 seq 3 的版本
```

注意：这一轮里 key2 两次更新都发生在水位之后，所以只回收了 key1 的一条早期版本。
若把历史安排在水位两侧（例如 2.3 把更新插入读者前后各一批，得到 `removed=4`），
部分回收会更明显。所以 `pinned=true` **不等于**零进展；2.2 的 `removed=0` 只是因为所有历史都年轻于水位。

### 2.5 陈旧请求水位：`pinned=false` 的成功空转

种子 3 行（seq 2），请求 horizon=2，随后做 5 次更新（head=7）：

```
POST /vacuum {horizon:2} -> {"horizon":2,"removed":0,"retained":8,"pinned":false}
POST /vacuum {horizon:7} -> {"horizon":7,"removed":5,"retained":3,"pinned":false}
```

`pinned=false` 只说明“没有读者把水位压得比请求值更低”，**不说明请求值追上了 head**。
以“先用 `stats.sequence` 取一个值、稍后再发 vacuum”的方式运维时，这两者会自然错位。

### 2.6 墓碑：计数、可见性与地板

4 行种子后开读者（snapshot=2），删除一个不存在的键 99，再删除存活键 2：

```
before:  keys=5  versions=6  可见行=3
POST /vacuum {horizon:head} -> {"horizon":2,"removed":0,"retained":6,"pinned":true}
关闭读者后再 vacuum        -> {"horizon":5,"removed":3,"retained":3}
after:   keys=3  versions=3  只剩 int:1/int:3/int:4
```

- **墓碑是一条版本**，且**墓碑链在删除前计入 `keys`**（键 99 从未存在，也占用 1 个 key / 1 个 version）。
- 删除“不存在的键”完全合法，且在高频重试路径上会持续产生墓碑版本。
- 墓碑一旦越过水位即整链删除；因此删除某键后 `versions` 可能反而下降。

### 2.7 统计口径：四个数字不是一回事

种子 4 行 + `by_amp` 索引，把 key1 从 10 改成 999：

| 事实 | 值 | 含义 |
| --- | --- | --- |
| `versions` | 5 | 版本条数（含历史） |
| `keys` | 4 | 版本链条数（含墓碑链） |
| `index_entries.by_amp` | 5 | 非墓碑版本的索引条目（含历史值） |
| 可见行 | 4 | 当前快照下的真实行数 |

查询侧同样有四个独立事实：

- `plan.candidates`：访问路径候选键数。`sample=1` 的旧值用索引命中 1 个候选；`force_scan` 时为 4（该关系的键数）。
- `scanned`：存在可见行并进入残余判断的行数。
- `matched`：限制输出前的匹配数。
- `rows`：实际输出。

一个针对已不存在的旧值 `amplitude = 10` 的索引查询给出 `candidates=1, scanned=1, matched=0`：
**候选存在、被扫描、但不匹配**。若把 `candidates` 或 `scanned` 当作“还剩多少行”，会直接误判回收效果。
索引条目是“历史值到主键”的候选集合，因历史值未回收而膨胀：

另一组小样本（3 行种子，振幅 10/20/30，各自带索引）：

```
重写 key1 为同一个值 10 两次: versions 5, index_entries 3（同值同键去重）
把 key2 也改成 10:            versions 6, index_entries 4
回收后:                        versions 3, index_entries 3
```

`versions` 与 `index_entries` 增长并不同步；只有 vacuum 触发 `index.Build` 后候选才会收缩。
同值同键的重写会增长 `versions` 但**不**增长索引条目，因此二者不能互相替代。

### 2.8 排除重启/恢复造成的假象

读者钉住时：versions=7、keys=4、horizon=3（seed 4 行 + update ×2 + delete）。
执行 `POST /checkpoint` 后停止进程，再在**同一目录**重启：

```
重启后: active=[]  sequence=7  horizon=3  keys=4  versions=7
备份图像逐链审计: 7 个版本，0 违规
```

- 保留的版本是**持久状态**，不是某个会话的残影：重启后依然存在。
- 但 `active` 为空，说明**钉住者不持久**；重启后 vacuum 立刻可以推进到 head，`removed=4`。
- 用备份 `-restore` 到新目录：`active=[]`、`horizon/sequence/versions` 与源库一致；
  `GET /changes?after=<restore_base-1>` 返回 410，`after=0` 同样 410——变更历史从恢复基点开始。
- 旧读者的快照号在重启后**不可再寻址**：如果重启前没有读过它，重启后 `at=<旧 snapshot>` 返回 410；
  已发出的事务句柄在重启后为 404。

**结论：重启不是“回收手段”，只是清除钉住状态；它不改变水位，也不能让已淘汰的历史读复活。**

### 2.9 满会话与写入压力（`sessions` 场景）

种子 3 行（seq 2），把 key1 更新一次（seq 3），然后开 128 个只读事务（全部 snapshot=3）：

```
active=128
POST /transactions (read_only=false) -> 429 capacity
关闭 1 个读者 -> 写事务成功提交 (writes=1, seq 4)
POST /vacuum {horizon:4} -> {"horizon":3,"removed":1,"retained":4,"pinned":true}
关闭剩余 127 个读者
POST /vacuum {horizon:5} -> {"horizon":5,"removed":1,"retained":3,"pinned":false}
```

- **空转的读者会同时占满会话预算并钉住水位**。
- 释放 1 个槽位只恢复“能开新事务”，不恢复“能推进回收”；128 个读者里只要还剩 1 个最小快照者，水位就停在 3。
- 注意与 2.2 的差别：本例在开读者**之前**已经把水位能推进的部分写完了，所以关掉读者后还需要再一次 vacuum 才能把版本降到 3。

### 2.10 维护调用的代价（钉住时最不划算）

规模与实测（每轮全量重写所有键）：

| 规模 | 钉住 vacuum | 回收量 | 解除钉住后 vacuum | 回收量 |
| --- | --- | --- | --- | --- |
| 1000 键 / 11k 版本 | 44.9 ms | 0 | 4.5 ms | 10,005 |
| 3000 键 / 33k 版本 | 396.9 ms | 0 | 8.8 ms | 30,000 |
| 8000 键 / 40k 版本 | 542.8 ms | 0 | 27.3 ms | 32,000 |

钉住时的调用**没有任何回收收益，却要在全局写锁内做一次全量索引重建**。
并发压测下，一次 115–400 ms 的钉住 vacuum 会把单次提交的最大停顿拉到同量级
（基线 p50 commit ≈ 5–6 ms；vacuum 期间 p50 不受影响，但 max ≈ 120–400 ms）。
反复调用不会变便宜：连续三次钉住 vacuum 分别耗时 50/46/48 ms，`removed` 都是 0。

对照实验（同样 3000 键 / 33k 版本，**不建索引**）：

| 场景 | 钉住 vacuum | 解除钉住后 vacuum |
| --- | --- | --- |
| 无索引 | 4.3 ms（removed=0） | 3.2 ms（removed=30,000） |
| 有 `by_amp` 索引 | 396.9 ms（removed=0） | 8.8 ms（removed=30,000） |

可见停顿主要来自 `index.Build` 对**全部历史版本**的重建，而不是版本链裁剪本身；
索引越多、历史越深，空转代价越高。没有索引时，钉住 vacuum 接近免费。

> 说明：本机数字仅用于量级比较，不作为产品性能承诺。

### 2.11 一个真实的空间压力后果：镜像上限

固定老读者 + 大量历史大值（`text` 列 64KiB，1100 行各两份历史；`image_cap` 场景）：

```
versions=2200  keys=1100
POST /vacuum {horizon:head} -> pinned=true, removed=0
POST /checkpoint -> 503 unavailable: checkpoint exceeds image limit
GET  /backup     -> 429 capacity: backup exceeds 128MiB
写入仍然成功
关闭读者 -> vacuum removed=1101, versions=1100
POST /checkpoint -> 200
```

历史版本被旧读者钉住时，**回收不能发生，镜像体积无法下降**，直至检查点/备份触碰 128MiB 上限。
写入路径不因此失败，磁盘日志也不会因 vacuum 缩小（见下节）。

### 2.12 日志与镜像的增长方向正好相反

```
vacuum 前: journal=436,667 B   backup=341,017 B
钉住 vacuum: journal=436,853 B  backup 不变
解除后:    journal=436,916 B   checkpoint/backup=19,046 B
```

`versions` 能降下来，**journal（日志）磁盘占用只会增加**（每条 vacuum 记录都要写一帧并 fsync）。
`checkpoint` 与 `backup` 反映回收后的状态，会显著缩小。

## 三、根因链

```
POST /vacuum {horizon:H}
  └─ engine.Vacuum                       maintenance.go:73
       ├─ 拒绝 H > sequence 或 H < horizon                    :79
       ├─ effective = H
       ├─ for tx in transactions: effective = min(effective, tx.Snapshot)   :82-87
       ├─ publish(vacuum record, horizon=effective)           :90   ← 先把水位写进日志
       │     └─ apply → store.Vacuum(effective) + d.horizon = effective    replay.go:56-59
       ├─ removed = beforeVersions - afterVersions            :88,93
       ├─ indexes = index.Build(catalog, store)               :94   ← 全量重建，持写锁
       └─ Pinned = effective < H                              :95
```

关键点逐条对应：

1. **水位只由“最小活动快照”决定**（`maintenance.go:82-87`）。
   只要存在一个 snapshot=S 的事务，effective ≤ S；S 之后的一切写入都在水位之上。
2. **水位只进不退**（`maintenance.go:79`）。
   一次低水位的成功调用会把 `horizon` 永久推进到 effective；之后再发更高的请求值只能继续前进。
   本例中把水位推进到 3 之后，`at=2` 立即变为 410，尽管相关版本仍被老读者持有。
3. **版本保留规则按链计算**（`versions/vacuum.go:14-26`）：
   - 保留所有 `Sequence > horizon` 的版本；
   - 再保留水位处或之前的最后一个版本（`keep`）；
   - 若最新版本本身是水位内的墓碑，则整链删除（`:18-21`）。
   因此最小保留量 = 每个存活链 1 版，**与 keys 同阶**；`versions ≈ keys + 水位之上的历史深度`。
4. **`versions` 是计数不是空间**。`Store.Count`（`store.go:81-89`）累加链长；
   `removed`/`retained` 也都是版本条数。索引条目另行统计（`index/registry.go:10-23`）。
5. **`pinned` 只比较 effective 与 H**（`maintenance.go:95`）。
   它既不表示“是否有读者”，也不表示“请求值是否等于 head”。
6. **钉住调用仍然做全量重建**（`maintenance.go:94`，存在索引时才有实际开销），且整个函数持 `d.mu.Lock()`（`:74`）。
   这就是“成功但没有收益”的调用的代价来源。
7. **读取端把 horizon 当作下界**（`read.go:32`）：`at < horizon` 一律 410。
   所以推进水位**不可逆地**缩小 `at` 查询窗口，即使物理版本尚未回收。
8. **重启只清除会话，不清除水位**：`active` 不落盘，`horizon`/`Store` 落盘（`maintenance.go:13-15`、`database.go:76-83`）。
   于是重启后旧读者消失、水位保持，下一次 vacuum 可以立刻推进——但已被淘汰的快照不可复活。

## 四、影响范围

| 受影响面 | 表现 | 证据 |
| --- | --- | --- |
| 空间回收 | 旧读者钉住期间 versions 只增不减；镜像可触碰 128MiB 上限 | 2.2 / 2.11 |
| 写入可用性 | 钉住 vacuum 持全局写锁做全量索引重建，提交最大停顿与之同量级 | 2.10 |
| 会话预算 | 遗留读者可占满 128 会话，导致新事务（含写事务）429 | 2.9 |
| 历史读窗口 | 任何一次 vacuum 永久抬高 horizon；`at` 下界不可回退 | 2.3 / 2.5 / 3.2 |
| 统计判读 | `versions`/`keys`/`index_entries`/`candidates`/`scanned`/`matched` 含义不同 | 2.7 |
| 磁盘 | vacuum 不缩小日志，反而追加记录 | 2.12 |
| 重启/恢复 | 版本与水位持久，会话与钉住不持久；恢复实例历史从基点开始 | 2.8 |
| 索引正确性 | 历史候选在回收前是超集，回收后重建；`at` 查询结果与扫描一致（53 个快照逐点核对无差异） | 2.7 / 验证脚本 |

不受影响（属正确保护，无需“修复”）：

- 老读者在其快照上读到旧值；
- `at == horizon` 可读、`at < horizon` 410；
- 墓碑在越过水位前保留旧行可见性；
- 唯一约束、提交冲突路径不留下残余钉住（冲突/唯一失败后 `active` 为空）。

## 五、可操作的处置及其数据访问代价

前提：用户既不能无差别结束合法长读，又需要在明确空间压力下继续写入。
下面按“先判断、后动作”的顺序排列，并标注代价。

### 第 0 步：先判断属于哪一类（零成本）

```sh
curl -s $URL/stats
```

- `active` 里最小的 `snapshot` 就是当前水位上限；对照 `horizon` 判断是否被钉住。
- 若 `horizon == 请求值` 且 `removed=0`：请求值没追上 head（类别 B），用新的 `sequence` 重发即可。
- 若 `horizon < 请求值`：被活动快照钉住（类别 A）；去 `active` 找出 `snapshot == horizon` 的会话。
- 若要判断“还有多少可用空间”，看 `keys`（存活键数）与 `index_entries`，不要看 `versions`。
- 判断可见行数用实际查询的 `matched`，不要用 `candidates`/`scanned`。

### 处置选项（可组合）

| 选项 | 做法 | 前提 | 数据访问代价 | 风险 |
| --- | --- | --- | --- | --- |
| 1. 保持保护，只重发 | 用当前 `sequence` 重发 `{horizon: sequence}` | 无活动读者 | 一次全量索引重建（有索引时）+ 全局写锁 | 无 |
| 2. 定向关闭最小快照读者 | 只对 `snapshot == horizon` 的会话调 `rollback`（读事务也可 `commit`） | 该长读可结束或可被替代 | 关闭本身 O(1)；恢复回收仍需一次 vacuum | 该读者后续请求 404；若仍在用会打断合法长读 |
| 3. 重启实例 | 优雅停止后重启 | 可接受短暂停机 | 清除全部会话与钉住；`horizon` 不变；回放日志随数据量增长（实测 62 ms–1.7 s 量级） | 所有会话失效；无法恢复已淘汰的 `at` 窗口 |
| 4. 备份→还原到新目录 | `GET /backup` → `-restore` | 需要一份独立、已回收的副本 | 生成/校验/安装全量图像；源库继续持锁 | 只解决“副本的空间”，源库仍需处置；受 128MiB 上限 |
| 5. 施加写入背压/限流 | 在应用侧限制写入速率或分批提交 | 有明确空间压力 | 不改变回收语义 | 若仍钉住，回收集压仍会增长 |
| 6. 保留 `at` 窗口的运维约定 | 把 `at` 查询限制在 `horizon..sequence`，并记录每次 vacuum 的 `horizon` | 无 | 零成本 | 不能恢复已淘汰的快照 |
| 7. 避免空转调用 | 只在 `horizon` 能推进时调用 vacuum | 无 | 省去每次 ~50–550 ms 的写锁停顿与日志增长 | 需要外部调度判断 |

**不推荐**：

- 用“结束任意一个长读”来期望回收——只有最小快照者有效（2.3）。
- 用 `pinned=false` 作为成功判据——它不表示追上 head（2.5）。
- 用 `versions` 下降作为“空间已释放”的唯一判据——它不反映日志/镜像（2.12）。
- 把重启当作“修复”——它只清会话，不改水位；已淘汰的快照不可恢复（2.8）。

### 处置与锁代价的量化对比（8000 键 / 40k 版本）

| 动作 | 耗时量级 | 写锁影响 | versions | 镜像 |
| --- | --- | --- | --- | --- |
| 钉住时 vacuum | 543 ms | 全程持有 | 40,000 → 40,000 | 不降 |
| 关闭最小快照读者 | 0.4 ms | 极短 | 不变 | 不变 |
| 关闭后 vacuum | 27 ms | 全程持有 | 40,000 → 8,000 | 降 |
| checkpoint（回收后） | 29 ms | 全程持有 | 不变 | 3.3 MB → 0.75 MB |

（同一组数据在 2000 键 / 22,000 版本时：钉住 vacuum 175 ms、关闭读者 0.35 ms、回收 vacuum 4.8 ms、checkpoint 10.5 ms。）

## 六、核验材料

### 6.1 可执行脚本

`examples/vacuum_diagnose.py`：12 个场景，全部只走公开 HTTP，每个场景独立空目录、自带断言，输出单行 JSON。

```sh
go build -o bin/laminad ./cmd/laminad
python3 -I examples/vacuum_diagnose.py --scenario all
```

| 场景 | 验证内容 |
| --- | --- |
| `baseline` | 无读者时一次回收到底；`removed+retained` 守恒；图像保留规则；`at` 边界 |
| `pinned` | 旧读者钉住；成功但 `removed=0`；钉住期间写入扩大积压；水位推进使旧 `at` 不可寻址 |
| `pinned_partial` | 钉住时仍回收水位之下的历史；读者快照完整 |
| `stale` | 陈旧请求水位 `pinned=false` 且零回收；追到 head 后才回收 |
| `tombstones` | 墓碑计入 keys/versions；墓碑链在水位前保留旧行可见性 |
| `stats` | `candidates`/`scanned`/`matched`/`versions`/`keys`/`index_entries` 互相区分 |
| `accounting` | `versions = 水位之上版本数 + 每条存活链 1 个地板`；索引条目排除墓碑 |
| `restart` | 版本与水位持久、会话不持久；重启后 vacuum 可推进；图像审计无违规 |
| `backup` | 冻结图像保留版本与水位；恢复后 `[horizon, sequence]` 可读；变更历史从基点开始 |
| `cost` | 钉住 vacuum 无回收仍重建全量索引；重复调用不降本；日志不缩、镜像缩 |
| `sessions` | 128 会话上限；满会话拒绝写事务；释放槽位恢复写入但不恢复回收 |
| `image_cap` | 钉住历史超过 128MiB：`checkpoint` 503、`backup` 429，写入仍成功，回收后恢复 |

### 6.2 逐链保留规则审计

脚本内 `retention_audit()` 直接从 `GET /backup` 的 `payload` 解析版本链，独立重算应保留的版本数：
墓碑链（最新版本为水位内墓碑）整链消失；其余链保留 `sequence > horizon` 的全部版本，外加至多 1 个水位内版本。
对基线、部分回收、墓碑、重启前后四组图像均得到 `violations=[]`，且 `versions_by_image` 与 `/stats` 的 `versions` 一致。

### 6.3 本次调查中确认的相关事实

- 会话上限 128 是总数上限（`Begin` 检查 `len(d.transactions)`），与只读/读写无关；写集上限 1000 键是另一条独立预算。
- 冲突与唯一约束失败的事务被直接关闭，不留下钉住（失败路径实测 `active=[]`）。
- 索引候选在 53 个历史快照上与强制扫描结果逐点一致（超集 + 可见性重判，未观察到陈旧候选造成的错误行）。
- `index.Build` 遍历全部历史版本（墓碑跳过），因此钉住时的重建代价随 `versions` 而非可见行数增长。
- 读取路径 `Query` 全程持 `d.mu.Lock()`（`read.go:9`），所以 vacuum 的停顿会直接传导到查询与提交。

## 七、建议的后续工作（不属于本次调查范围）

1. 让 `Vacuum` 在 `effective == d.horizon` 时跳过索引重建与日志写入，避免无收益的写锁停顿。
2. 在 `VacuumResult` 中区分 `requested`、`effective`、`head`，并暴露 `pinned_by`（最小快照事务 ID），减少误判。
3. 评估“最小快照读者超时/配额”策略，或事务级 `max_snapshot_age`，但需显式说明这会取消对长读的保证。
4. 为 `at` 窗口与 `horizon` 的关系补文档：任何一次 vacuum 都会永久缩小可寻址区间。
5. 考虑 `checkpoint` 在 128MiB 上限下的降级路径（当前直接 503，而写入仍在继续）。
