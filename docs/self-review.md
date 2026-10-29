# 回顾、打分与日报

> 这一套东西只做三件事：**记账**（token / 对话）、**复盘**（每日只读日报）、**改进**（你打分 → 生成修改意见 → 你通过）。
> 所有数据都在本地，不会自动改任何配置或人设。

打开面板：<http://127.0.0.1:8899/brain>，看 **Review 打分 · 日报** 卡片。

---

## 1. 数据存在哪

一个本地 SQLite 文件：`resources/data/robot.db`（删掉就从零开始）。

| 表 | 内容 |
|---|---|
| `llm_calls` | 每次大模型调用的 prompt / completion token（区分真实与估算） |
| `turns` | 每一轮对话：你说什么、她回什么、耗时、是否被打断、本轮 token |
| `reviews` | 你对某轮回复的打分（1~5、标签、备注），每轮最多一条 |
| `proposals` | 根据打分生成的修改意见，以及"通过/忽略"状态 |
| `capability_runs` | 本地能力的调用记录（成功/失败/耗时/结果摘要） |
| `journals` | 每日日报的元信息（正文在 `resources/memory/journal/*.md`） |

> 隐私：全部在本机。只有生成"日报观察"和"修改意见"时，会把**对话片段/评分**发给 LLM
> （和平时聊天用的是同一个 API）。不想发就关掉 `system.self_review.enable`。

---

## 2. Token 记账

- **非流式调用**：直接用服务商返回的 `usage`；
- **流式调用**：尝试让服务商在最后一个 chunk 返回 usage（DeepSeek/OpenAI 兼容接口支持）；
  不支持时自动退回"按字符估算"，并标记 `estimated=1`；
- 面板顶部显示**今日 token**（总数 / prompt / completion），日报里也有。

之所以要区分"真实 vs 估算"：做成本核算时，估算值只能看趋势，不能当账单。

---

## 3. 每日日报（只读）

- 默认每天 **22:00 之后**生成一次，落在 `resources/memory/journal/YYYY-MM-DD.md`；
- 内容：对话轮数、token、当天评分与低分片段、能力调用次数、一段"观察与建议"；
- **只写日报，不改任何设置**。没有可用的大模型时也会生成（只是少一段观察）；
- 面板上可以点「生成今天的日报」手动补，或直接打开 `/journal/2026-10-29` 查看。

---

## 4. 打分 → 修改意见 → 你决定

1. 点 **「抽 5 句打分」**：从最近 14 天里随机抽还没打分的对话（你一句、她一句）；
2. 选 1~5 分，可填标签（太长/太冷/出戏…）和备注，点保存；
3. 打了一些分之后，点 **「生成修改意见」**：把最近的评分交给大模型，产出一组
   **可执行的中文调整建议**（最多 3 条）；
4. 每条意见有 **通过 / 忽略**：
   - 通过 → 追加到 `self_state` 的「近期调整约定」，**下一轮对话立刻生效**（写进 system prompt）；
   - 忽略 → 只改状态，不生效。
5. 约定可以随时在 `resources/memory/self_state.json` 的 `adjustments` 里手动删。

> 为什么不让它自动应用？因为"她觉得自己变好了"和"你觉得她变好了"是两件事。
> 通过这一步是你保留的唯一否决权。

---

## 5. 给之后的微调准备数据

已打分的数据就是一份带偏好标注的对话集。导出成 JSONL：

```bash
ZRExistence_venv\Scripts\python.exe -c "from framework import telemetry; telemetry.init('resources/data/robot.db'); print(telemetry.export_reviews_jsonl('resources/data/reviews.jsonl'))"
```

每行包含：`turn_id / ts / score / tags / comment / user_text / reply`。
样本量够大之后，可以直接拿去做 SFT / DPO（高分 vs 低分）。

---

## 6. 配置

`resources/config.yaml` → `system.self_review`：

| 键 | 默认 | 说明 |
|---|---|---|
| `enable` | `true` | 总开关（关掉后不记 token、不生成日报） |
| `db_path` | `resources/data/robot.db` | 遥测库路径 |
| `journal_dir` | `resources/memory/journal` | 日报目录 |
| `journal_hour` | `22` | 几点后生成；`25` = 不自动生成 |
| `review_sample_size` | `5` | 每次抽几句 |
| `review_sample_days` | `14` | 抽样时间范围 |
| `proposal_max_items` | `3` | 每次最多几条意见 |
| `backfill_history` | `true` | 首次启动把已有历史补录进库，立刻有句子可打分 |

---

## 7. 相关文件

| 文件 | 作用 |
|---|---|
| `framework/telemetry.py` | SQLite 数据层（token/对话/评分/提案/能力记录/日报元信息） |
| `framework/journal.py` | 日报的 prompt 与 Markdown 渲染（纯函数） |
| `pipeline/llm/llm_sync.py` | token 计量挂载点 |
| `bot.py` → `_ctl_review_*` / `_ctl_*proposal*` / `_write_journal` | 业务逻辑 |
| `resources/memory/journal/` | 日报正文 |
| `resources/memory/self_state.json` → `adjustments` | 你通过的调整约定 |
