# 控制台与本地 API

> 面板：<http://127.0.0.1:8899/brain>
> 能力：切模式、开关麦克风、增删待办/目标、给她发消息、让她念一句、停止说话。
> 语音说同样的话也能触发（不需要开面板）。

---

## 1. 面板上的「Control 控制」卡片

打开 <http://127.0.0.1:8899/brain>，滚到中间那块 **Control 控制**：

| 区域 | 能做什么 |
|---|---|
| 主动模式 | 一键切 **安静 / 普通 / 陪聊 / 自动**，当前模式高亮 |
| 麦克风 | 开关麦（等价于 `F8`），按钮显示当前状态 |
| 停止说话 | 立刻停播并取消在途生成（等价于"插话打断"） |
| 待办清单 | 输入框回车新增；点方框勾选完成；点 `×` 删除 |
| 长期目标 | 新增 / 删除（会写进下一次对话的 system prompt） |
| 和她说话 | **发送** = 当作你说了一句话，走完整回复（含朗读）；**只念这句** = 直接让她朗读，不经过 LLM |

所有改动都会立刻落盘（`resources/memory/self_state.json`），并刷新面板。

---

## 2. 语音也能做同样的事

不需要打开面板，直接说（整句匹配，命中后不会送去 LLM）：

| 想做什么 | 说 |
|---|---|
| 切安静 | `安静`、`安静一点`、`专注模式`、`别说话了` |
| 恢复 | `恢复`、`可以说话了`、`继续说话` |
| 切普通 / 陪聊 / 自动 | `普通模式`、`陪我聊天`、`自动模式` |
| 加待办 | `添加待办 买牛奶`、`加个待办：买牛奶`、`把写周报加入待办` |
| 完成待办 | `完成待办 买牛奶`、`待办买牛奶完成了` |
| 删待办 | `删除待办 买牛奶`、`把买牛奶从待办里删掉` |
| 加 / 删目标 | `添加目标 每天跑步`、`删除目标 每天跑步` |

> 判定规则在 `framework/local_commands.py`，只认**很严格的句式**，宁可漏识别交给 LLM，
> 也不会把正常聊天误判成命令。加待办/删待办成功后她会应一声"记下了。/ 好。"

---

## 3. HTTP API（给脚本/手机快捷方式用）

基础地址：`http://127.0.0.1:8899`

| 方法 | 路径 | 请求体 | 说明 |
|---|---|---|---|
| GET | `/control/state` | — | 当前模式、麦克风、忙碌、待办、目标 |
| POST | `/control/mode.set` | `{"mode":"focus"}` | `auto` / `focus` / `normal` / `companion` |
| POST | `/control/mode.resume` | `{}` | 从安静恢复；不在安静则回到 `auto` |
| POST | `/control/mic.set` | `{"on":true}` | 开/关麦克风 |
| POST | `/control/todo.add` | `{"text":"买牛奶"}` | 也有 `todo.done` / `todo.undone` / `todo.remove` |
| POST | `/control/goal.add` | `{"text":"每天跑步"}` | 也有 `goal.remove` |
| POST | `/control/ask` | `{"text":"今天好累"}` | 当作你说的话，走完整一轮回复 |
| POST | `/control/speak` | `{"text":"晚安"}` | 只朗读，不经过 LLM、不进历史 |
| POST | `/control/stop` | `{}` | 停播 + 取消在途生成 |

返回统一是：

```json
{"ok": true,  "result": ...}
{"ok": false, "error": "具体原因"}
```

示例：

```bash
curl -s http://127.0.0.1:8899/control/state
curl -s -X POST http://127.0.0.1:8899/control/mode.set -H "Content-Type: application/json" -d "{\"mode\":\"focus\"}"
curl -s -X POST http://127.0.0.1:8899/control/todo.add  -H "Content-Type: application/json" -d "{\"text\":\"买牛奶\"}"
```

---

## 4. 安全

资源服务器监听 `0.0.0.0`，但 `/control/*` **默认只允许本机**（`127.0.0.1` / `::1`）。

想让手机或局域网里的脚本也能控制：

```yaml
service:
  res_server:
    allow_remote_control: true
```

> 打开后同网段的任何人都能开关你的麦克风、让她说话。只在可信网络里这么用。

---

## 5. 怎么加一个新按钮 / 新接口

控制面就是一张动作注册表（`framework/control.py`）：

```python
from framework import control

control.register("my.action", lambda payload: {"hi": payload.get("text")})
# 之后 POST /control/my.action 就会调用它
```

机器人在启动时会注册这些动作（见 `bot.py` 的 `_register_control_handlers`）：

```
state, mode.set, mode.resume, mic.set, speak, ask, stop,
todo.add, todo.done, todo.undone, todo.remove, goal.add, goal.remove
```

加动作时只需要：注册函数 → 前端加一个按钮调 `POST /control/<动作>`。
`call()` 会把异常包成 `{"ok": false, "error": ...}`，不会把 500 抛给前端。

---

## 6. 相关文件

| 文件 | 作用 |
|---|---|
| `framework/control.py` | 动作注册表（`register` / `call` / `available`） |
| `framework/local_commands.py` | 语音短句 → 动作（纯函数，可单测） |
| `bot.py` → `_register_control_handlers` | 机器人具体能力 |
| `services/playground/res/res_server.py` | `/control/*` 路由 + 面板前端 |
| `resources/memory/self_state.json` | 待办 / 目标的持久化 |
