# Page07 重分析技术兼容性标准 v2

## 范围与批准边界

这是 `daily_report_regenerate` 的专项技术兼容性标准，不是原通用模型资格标准的三真人可读性认证。用户已授权收口真实重分析链；本标准不伪造、不回写、不替代旧 Human 评分。最终业务报告仍须用户审核采用，模型无权批准自己的报告。评估器不写 registry、不调用模型、不读取凭据、不自动登记准入。

身份：`page07-regenerate-qualification-pack/2.0`、`page07-regenerate-rules/2.0`。输出合同与 prompt 版本仍为 1.0；实际 prompt contract hash、采样 hash、AI schema、provider/model/version、完整 capability 必须与当前产品构造结果完全一致。运行前独立固定的 manifest pin 不得取自待验文件后自行当作外部信任证据。

## 唯一样板

`samples.json` 是版本化的 12 条合成重分析样板。其 samples 数组来自既有冻结侧车，原 base Gold、原报告、人工纠正、期望模块名和工作事实不变。fake SHA 明确属于合成素材，不能声称来自真实客户仓库。历史 `policy_version=v4` 只是样板构造元数据，实际调用策略由当前 manifest 冻结，不能用其替代当前 prompt 身份。

12 条基础样本加固定 BASE-JSTS-04、BASE-JAVA-02、BASE-PY-04 各 3 次重复，共 21 个唯一槽。不得缺项、混合旧运行、复用同一 provider response id、用单条诊断补齐或在失败后覆盖旧结果。

## 通过条件

1. 当前完整 21 条真实 provider receipt 全部成功，并与槽、claim、request/wire、model、usage、独立预固定 manifest pin 闭合。运行须保留非空 provider response id 和 runtime fingerprint。自签 JSON 哈希不能单独证明 provider 真实性：最终独立复审必须核对实际收集器、授权意图及执行证据。
2. 所有结果符合产品 regenerate 结构合同，引用只来自该样本及其人工纠正，模块名精确完整匹配；不做模糊指认。结构校验不是语义正确性的替代。
3. 复用原 `evaluate_qualification_run`，不改其阈值：Git 事实、无捏造、结构完整、来源区分、边界遵守 12/12；主要功能/阶段/实现范围至少 10/12；证据不足保守样本至少 4；三组重复各至少 2/3 核心一致且无矛盾。
4. 21 条均有绑定本轮 manifest/source/receipt hash 的独立 AI 审阅。Builder 自检不算独立审阅，缺项不填 true。审阅身份与观察真实性需 Main/独立 Reviewer 实际核实，字符串标签不是身份认证。
5. 三项专项逐条通过：纠正已处理或合理拒绝、原正确事实保留、无新增无依据结论。重复样本也须 facts/no-fabrication/structure/source/boundary 全通过且无 hard failure；不能仅借三元组稳定掩盖错误。重复三元组须等于真实 receipt 的唯一模块 feature/stage/scope。
6. 独立审阅不能声称确定性不允许的阶段/范围为 true；反向 false 可表达其他行语义矛盾，不由程序强行改为 true。

## 结果与消费接口

`evaluate_compatibility(manifest, sidecar, directory, reviews, *, pin, current_capability)` 只读返回带 hash 的报告。`compatibility_passed` 独立表达此专项是否通过。`original_harness` 和原 `qualification_status=human_gate_required` 保留；`human_readability_certified=false`、`human_readability_reviews=null`、`automatic_admission=false`。旧三真人门没有被宣称通过。

仅在独立复审上述证据、确认 exact 当前身份后，产品集成方可显式登记这一专项标准来源；不能把本结果改写成旧通用认证，也不能以测试伪收据登记真实模型。离线测试仅验证拒绝/计分行为，不证明真实模型质量。
