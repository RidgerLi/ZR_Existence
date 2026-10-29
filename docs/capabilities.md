# 本地能力（Capabilities）

> 往 `capabilities/` 目录扔一个 `.py`，它就会出现在面板上：可以**开关**、**试跑**、
> **看调用记录**，每次调用也会写进日志。不需要重启。

---

## 1. 一个能力长什么样

固定目录：`capabilities/`（可在 `system.capabilities.dir` 改）。

```python
# capabilities/my_skill.py
NAME = "my_skill"                      # 唯一名字
DESCRIPTION = "一句话说明这个能力干什么"  # 显示在面板上

def run(args: dict) -> str:
    # args 来自面板/HTTP 的 JSON（可能是空 dict）
    text = args.get("text", "")
    return f"收到：{text}"              # 返回值会显示在面板并写日志
```

三个要求：

| 名字 | 类型 | 作用 |
|---|---|---|
| `NAME` | str | 唯一标识；重名会被覆盖（按文件名排序，后者胜） |
| `DESCRIPTION` | str | 面板上的说明 |
| `run(args)` | 函数 | 实际逻辑；抛异常会被记为失败 |

文件以 `_` 开头的会被忽略（可以放 `_draft.py` 草稿）。

---

## 2. 面板上怎么用

打开 <http://127.0.0.1:8899/brain>，找到 **Capabilities 本地能力**：

- 每行显示：名称、说明、累计调用次数/平均耗时；
- 中间输入框是**运行参数（JSON）**，例如便签能力填 `{"text":"买牛奶"}`；
- **运行**：立即执行，结果出现在右上角提示里；
- **停用 / 启用**：状态持久化到 `resources/capabilities.json`，停用后拒绝执行；
- 点 **刷新** 或直接新增 `.py` 文件，列表会自动更新（目录 mtime 变化触发重载）。

---

## 3. HTTP 调用

```bash
# 列出能力
curl -s -X POST http://127.0.0.1:8899/api/capability.list -H "Content-Type: application/json" -d "{}"

# 运行
curl -s -X POST http://127.0.0.1:8899/api/capability.run \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"time_now\",\"args\":{}}"

# 开关
curl -s -X POST http://127.0.0.1:8899/api/capability.toggle \
  -H "Content-Type: application/json" -d "{\"name\":\"note\",\"enabled\":false}"
```

返回统一 `{"ok": true, "result": ...}` / `{"ok": false, "error": "..."}`。

---

## 4. 记录与日志

- 每次运行写一行 `[CAP] name ok ms=12 result=...` 到日志，面板日志可以选 **CAP** 分类查看；
- 同时落库（`capability_runs` 表）：时间、成功与否、耗时、结果摘要；
- 面板每行显示累计次数/平均耗时，失败也会计数。

调用记录不会自动清理；数据量大了直接删 `resources/data/robot.db` 里的表即可（或整库重置）。

---

## 5. 内置的两个示例

| 文件 | 说明 | 参数 |
|---|---|---|
| `capabilities/time_now.py` | 返回当前日期/星期/时间 | 无 |
| `capabilities/note.py` | 追加一行到 `resources/memory/notes.md` | `{"text":"..."}` |

---

## 6. 设计上的两个刻意选择

1. **只有显式调用，不会让大模型自动触发。**
   新增能力必须是你审过的、可预期的东西；否则"她能做的事"会变成黑箱。
   （以后如果想让她自动调用，可以在 `bot.py` 里把能力注册成 LLM 工具，但那是另一个决定。）
2. **能力是普通 Python 函数，没有沙箱。**
   等价于你自己在本机跑脚本——只放你信任的代码，不要从网上随便复制。

---

## 7. 相关文件

| 文件 | 作用 |
|---|---|
| `framework/capabilities.py` | 扫描、加载、开关、执行、记录 |
| `capabilities/*.py` | 能力本体 |
| `resources/capabilities.json` | 开关状态 |
| `framework/telemetry.py` | 调用记录落库 |
| `framework/log_monitor.py` | 日志分类（CAP） |
