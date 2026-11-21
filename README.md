# 阅读联合研究的受限统计与共享额度

本地 Python 应用：研究员在机构批准的用途内申请差分隐私统计，审核人批准后
发布可解释的加噪结果，资料方管理授权范围，管理员核对披露与额度。
**所有机构与个人记录均为合成资料，应用不连接任何真实机构账户。**

## 交付内容

```
fedread/                 应用包（仅 Python 3.11 标准库 + SQLite）
  config.py              指标/分组/状态机等口径常量
  dp.py                  单位邻接、L1 敏感度、拉普拉斯机制、确定性噪声
  engine.py              受控联合聚合（唯一读取 records_raw 的模块）
  storage.py             SQLite 模式、IMMEDIATE 事务、只追加审计
  bootstrap.py           合成资料导入与本地角色初始化
  service.py             角色操作、授权依赖、共享额度、发布/复用/撤回/恢复
  cli.py                 本地命令行界面（JSON 输出，错误脱敏）
scripts/demo_scenarios.py 四个承诺交会场景的端到端演练（带断言）
tests/                   44 个单元/集成测试
docs/隐私保证.md          邻接、敏感度、组合、精度与事务保证的完整说明
docs/操作指南.md          四个本地角色的操作手册与状态机
materials/               原始合成资料（保持不变，应用不写回）
```

## 核心承诺与实现位置

| 承诺 | 实现 |
| --- | --- |
| 统计单位是保护单位而非行数；跨机构同人/同家庭先合并 | `engine.compute` 的单位分区 + `contribution_limit` 确定性截断 |
| 有明确保证的 DP，而非加小噪声/藏小单元 | `dp.py` 拉普拉斯机制，L1 敏感度上界 + (ε,0)-DP，线性顺序组合 |
| 预览可申请结构与预计精度，不返回真实统计 | `FedReadService.preview`，单元只来自计划白名单 |
| 共享额度不能按机构授权相加；同计划请求竞争同一预算 | `ledger` + 单事务发布，并行竞争仅一个成功，失败零扣账 |
| 同形同版本结果复用；重试不重抽噪声 | 发布指纹 + `DeterministicLaplace`（SHA-256 域分隔派生） |
| 批准不等于发布；送达与扣账同生共死 | `BEGIN IMMEDIATE` 单事务写 release/ledger/状态 |
| 中断续办不重复扣账、无“已送达未扣账” | `arm-crash` 双故障点 + `recover` 按持久事实收敛 |
| 撤回按真实依赖停止未发请求；已发布保留不退费、不株连 | `withdraw_grant` 按 (计划, 版本) 依赖处置 |
| 管理员可核对披露↔授权/版本/贡献/额度；研究员只见获准结果 | `disclosure_report`、`audit_trail` 与角色访问控制 |

## 快速开始

```sh
python3 -m fedread.cli init --materials materials     # 初始化本地库
python3 -m fedread.cli catalog --actor U4             # 查看计划/版本/授权
python3 -m scripts.demo_scenarios                     # 四个场景演练
python3 -m unittest discover -s tests -v              # 全部测试
python3 inspect_materials.py                          # 原始资料结构检查
```

具体角色命令见 [docs/操作指南.md](docs/操作指南.md)，
隐私口径与精度限制见 [docs/隐私保证.md](docs/隐私保证.md)，
资料字段契约见 [资料契约.md](资料契约.md)。

运行生成的数据库与导出写入 `state/`（已在 .gitignore），原始资料保留不变。
