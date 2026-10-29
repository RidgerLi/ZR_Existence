import json
import os
from pathlib import Path
from typing import Any, Dict
from uuid import uuid4

from flask import Flask, abort, send_file, request, jsonify, Response
from loguru import logger
from openai import BaseModel
from typeguard import typechecked

from common.concurrent.abs_runnable import ThreadRunnable
from common.io.file_sys import fs
from common.io.file_type import AudioFileType
from common.utils.audio_util import get_audio_real_format
from event.event_data import DeviceScreenCapturedEvent, DeviceMicrophoneVADEvent
from event.event_emitter import emitter
from framework import control
from manager.config_manager import get_config

RESOURCE_TYPES = {
    "audio": "audio",
    "image": "image",
    "video": "video",
    "model": "model",
}

_config = get_config()


class HTTPResponseBody(BaseModel):
    code: int = 0  # 0 means successful operation
    message: str
    data: Any = None


class _AudioMetadata(BaseModel):
    channels: int
    sample_rate: int


# Path => file_id
_files: Dict[str, str] = {}


@typechecked
def register_file(path: str | Path) -> str:
    global _files
    path = Path(path).absolute()
    assert path.exists(), f"No such file: {path}"

    file_id = _files.get(str(path), None)
    if file_id is None:
        file_id = str(uuid4())
        _files[str(path)] = file_id

    return file_id


class ResourceServer(ThreadRunnable):
    def name(self):
        return "ResourceServer"

    def stop(self):
        super().stop()

    def __init__(self, host: str, port: int):
        super().__init__()
        self.host = host
        self.port = port
        self.app = Flask(__name__)
        self.init()

    def init(self):
        @self.app.route('/resource/temp/<resource_type>/<filename>')
        def serve_resource(resource_type: str, filename):
            """
            根据请求的资源类型和文件名，从指定文件夹中提供文件。
            """
            # 检查资源类型是否有效
            if resource_type not in RESOURCE_TYPES:
                abort(404, description="Invalid resource type")

            # 构造文件路径
            file_path = str(os.path.join(resource_type, filename))
            file_path = os.path.join(fs.temp_dir, file_path)

            # 检查文件是否存在
            if not os.path.exists(file_path):
                abort(404, description="File not found")

            return send_file(file_path)

        @self.app.route('/resource/file')
        def handle_resource_file():
            global _files
            file_id = request.args.get('file_id')
            assert file_id is not None

            for path, id in _files.items():
                if id == file_id:
                    logger.info(f"File (id={file_id}) is found: {path}")
                    return send_file(path)

            logger.warning(f"No file (id={file_id}). Current files map is: \n{json.dumps(_files, indent=4)}")

            abort(404, description="File not found")

        @self.app.route("/playground/camera", methods=["POST"])
        def camera_send():
            try:
                logger.info("Get camera image from client.")
                file = request.files.get("image", None)
                image_type = None
                if file.content_type == "image/png":
                    image_type = 'png'
                elif file.content_type == "image/jpeg":
                    image_type = 'jpeg'
                elif file.content_type == "image/jpg":
                    image_type = 'jpg'

                img_path = fs.create_temp_file_descriptor(prefix="imgcap", suffix=f".{image_type}", type="image")
                file.save(img_path)
                file.close()

                emitter.emit(DeviceScreenCapturedEvent(img_path=img_path, is_camera=True))
                return HTTPResponseBody(message="OK").model_dump()
            except Exception as e:
                logger.exception(e)
                return HTTPResponseBody(message="Failed to receive your image data.", code=1).model_dump()

        @self.app.route("/playground/microphone", methods=["POST"])
        def microphone_send():
            # channels: int
            # sample_rate: int
            # audio: bytes
            try:
                logger.info("Get microphone audio from client.")

                # Decode from JSON binary data to get metadata about audio file
                audio_metadata = request.files.get("metadata", None)
                assert audio_metadata is not None, "Invalid audio metadata"
                audio_metadata = audio_metadata.stream.read()
                audio_metadata = audio_metadata.decode("utf-8")
                audio_metadata = _AudioMetadata.model_validate_json(audio_metadata)

                # Read data from audio file
                file = request.files.get("audio", None)
                audio_data = file.stream.read()

                # Check audio data type
                if file.content_type == "audio/mp3":
                    audio_type = AudioFileType.MP3
                elif file.content_type == "audio/wav":
                    audio_type = AudioFileType.WAV
                elif file.content_type == "audio/ogg":
                    audio_type = AudioFileType.OGG
                else:
                    audio_type = get_audio_real_format(audio_data)

                if audio_metadata.channels > 2:
                    logger.warning(f"Is that right? \n{audio_metadata}")
                emitter.emit(DeviceMicrophoneVADEvent(speech=audio_data,
                                                      channels=audio_metadata.channels,
                                                      sample_rate=audio_metadata.sample_rate,
                                                      audio_type=audio_type))

                return HTTPResponseBody(message="OK").model_dump()
            except Exception as e:
                logger.exception(e)
                return HTTPResponseBody(message="Failed to receive your audio data.", code=1).model_dump()

        if _config.system.brain.enable and _config.system.brain.enable_dashboard:
            self._init_brain_dashboard()

    def _init_brain_dashboard(self):
        import logging

        from framework.brain import monitor
        from framework.memory import monitor as memory_monitor
        from framework import log_monitor

        # 大脑面板每 ~300ms 轮询一次 /brain/state 与 /brain/history，werkzeug 开发服务器默认
        # 会为每个请求打一条访问日志，导致刷屏。这里把 werkzeug 访问日志降到 WARNING 级，
        # 只保留真正的告警/错误（不影响本项目自身的 loguru 日志）。
        logging.getLogger("werkzeug").setLevel(logging.WARNING)

        # 把本进程日志收进环形缓冲，供面板 /brain/logs 展示。
        log_monitor.install_sink()

        @self.app.route("/brain/state")
        def brain_state():
            return jsonify(monitor.get_latest() or {})

        @self.app.route("/brain/history")
        def brain_history():
            return jsonify(monitor.get_history())

        @self.app.route("/brain/memory")
        def brain_memory():
            return jsonify(memory_monitor.get_latest() or {})

        @self.app.route("/brain/logs")
        def brain_logs():
            return jsonify(log_monitor.get_logs(
                category=request.args.get("category"),
                level=request.args.get("level"),
                q=request.args.get("q"),
                limit=int(request.args.get("limit") or 500),
            ))

        @self.app.route("/brain")
        def brain_dashboard():
            return Response(_BRAIN_DASHBOARD_HTML, mimetype="text/html")

        # ---- 本地控制面：面板按钮 / 外部脚本都走这里（见 framework/control.py）----
        def _control_allowed() -> bool:
            if getattr(_config.service.res_server, "allow_remote_control", False):
                return True
            return (request.remote_addr or "") in ("127.0.0.1", "::1", "localhost")

        @self.app.route("/control/state")
        def control_state():
            if not _control_allowed():
                abort(403)
            return jsonify(control.call("state"))

        @self.app.route("/control/<action>", methods=["POST"])
        def control_action(action: str):
            if not _control_allowed():
                abort(403)
            payload = request.get_json(silent=True) or {}
            return jsonify(control.call(action, payload))

        # ---- 通用 API：GET/POST /api/<action>，动作名与 control.available() 一致 ----
        @self.app.route("/api/<action>", methods=["GET", "POST"])
        def api_action(action: str):
            if not _control_allowed():
                abort(403)
            payload = dict(request.get_json(silent=True) or {}) if request.method == "POST" else {}
            for k, v in request.args.items():
                payload.setdefault(k, v)
            return jsonify(control.call(action, payload))

        @self.app.route("/journal/<day>")
        def journal_view(day: str):
            if not _control_allowed():
                abort(403)
            r = control.call("journal.get", {"day": day})
            if not r.get("ok"):
                abort(404)
            return Response((r.get("result") or {}).get("markdown", ""),
                            mimetype="text/plain; charset=utf-8")

    def start(self):
        super().start()
        self.app.run(host=self.host, port=self.port, debug=True, use_reloader=False)


_BRAIN_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Brain Monitor</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; font-family: ui-monospace, Consolas, monospace; background:#0d1117; color:#e6edf3; }
  header { padding:12px 18px; border-bottom:1px solid #21262d; display:flex; gap:18px; align-items:center; flex-wrap:wrap; }
  h1 { font-size:15px; margin:0; color:#7ee787; letter-spacing:1px; }
  .pill { padding:3px 10px; border-radius:999px; background:#161b22; border:1px solid #30363d; font-size:12px; }
  .pill b { color:#79c0ff; }
  .state-IDLE{color:#8b949e}.state-LISTENING{color:#79c0ff}.state-THINKING{color:#d2a8ff}.state-SPEAKING{color:#7ee787}
  .act-proactive{color:#ffa657}.act-reactive{color:#79c0ff}.act-idle{color:#484f58}
  main { padding:18px; display:grid; grid-template-columns: 1fr 1fr; gap:18px; }
  .card { background:#161b22; border:1px solid #21262d; border-radius:10px; padding:14px; }
  .card h2 { font-size:12px; margin:0 0 10px; color:#8b949e; text-transform:uppercase; letter-spacing:1px; }
  .bar { margin:8px 0; }
  .bar .lab { display:flex; justify-content:space-between; font-size:12px; margin-bottom:3px; }
  .bar .track { height:12px; background:#0d1117; border-radius:6px; overflow:hidden; border:1px solid #30363d; position:relative; }
  .bar .fill { height:100%; transition:width .15s linear; }
  .bar .thr { position:absolute; top:-2px; bottom:-2px; width:2px; background:#f85149; }
  .offline { opacity:.4; }
  canvas { width:100%; height:200px; display:block; }
  .legend { display:flex; gap:14px; font-size:11px; margin-top:6px; }
  .legend span::before { content:"\\2014  "; }
  .full { grid-column: 1 / -1; }
  h3.sub { font-size:11px; color:#8b949e; margin:12px 0 4px; text-transform:uppercase; letter-spacing:1px; }
  .kv { font-size:12px; }
  .kv .k { color:#8b949e; }
  .mem-sum { white-space:pre-wrap; background:#0d1117; border:1px solid #30363d; border-radius:6px; padding:8px; font-size:12px; max-height:140px; overflow:auto; }
  ul.goals { padding-left:18px; margin:6px 0; font-size:12px; }
  ul.todo { list-style:none; padding-left:0; margin:6px 0; font-size:12px; }
  ul.todo li.done { color:#6e7681; text-decoration:line-through; }
  ul.goals li.k, ul.todo li.k { color:#6e7681; list-style:none; }
  .turns { font-size:12px; max-height:220px; overflow:auto; }
  .turns .turn { margin:4px 0; line-height:1.45; }
  .turns .role-user b { color:#79c0ff; }
  .turns .role-assistant b { color:#7ee787; }
  .turns .role-system b { color:#8b949e; }
  .turns .ts { color:#6e7681; margin-right:6px; }
  .logs { font-family: ui-monospace, Consolas, monospace; font-size:11.5px; max-height:300px; overflow:auto; background:#0d1117; border:1px solid #30363d; border-radius:6px; padding:8px; }
  .logs .row { white-space:pre-wrap; line-height:1.4; }
  .logs .t { color:#6e7681; }
  .lv-INFO{color:#79c0ff}.lv-DEBUG{color:#8b949e}.lv-WARNING{color:#ffa657}.lv-ERROR{color:#f85149}.lv-SUCCESS{color:#7ee787}.lv-CRITICAL{color:#f85149}
  .btn { background:#21262d; color:#e6edf3; border:1px solid #30363d; border-radius:6px; padding:4px 10px; font:inherit; font-size:12px; cursor:pointer; }
  .btn:hover { border-color:#58a6ff; color:#79c0ff; }
  .btn.on { border-color:#7ee787; color:#7ee787; }
  .btn.active { background:#1f6feb; border-color:#1f6feb; color:#fff; }
  .btn.danger:hover { border-color:#f85149; color:#f85149; }
  .ctl-row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin:8px 0; font-size:12px; }
  .ctl-row input { flex:1; min-width:200px; background:#0d1117; border:1px solid #30363d; border-radius:6px; color:#e6edf3; padding:5px 8px; font:inherit; font-size:12px; }
  .ctl-grid { display:grid; grid-template-columns:1fr 1fr; gap:18px; }
  ul.todo li a, ul.goals li a { color:inherit; text-decoration:none; }
  ul.todo li .del, ul.goals li .del { color:#6e7681; margin-left:6px; }
  ul.todo li .del:hover, ul.goals li .del:hover { color:#f85149; }
  .msg { font-size:12px; min-height:16px; color:#8b949e; }
  select { background:#0d1117; border:1px solid #30363d; border-radius:6px; color:#e6edf3; padding:4px 6px; font:inherit; font-size:12px; }
  textarea { background:#0d1117; border:1px solid #30363d; border-radius:6px; color:#e6edf3; padding:6px 8px; font:inherit; font-size:12px; width:100%; box-sizing:border-box; }
  .review-item { border:1px solid #21262d; border-radius:8px; padding:10px; margin:8px 0; background:#0d1117; }
  .review-item .q { color:#79c0ff; font-size:12px; margin-bottom:4px; white-space:pre-wrap; }
  .review-item .a { color:#e6edf3; font-size:12px; margin-bottom:8px; white-space:pre-wrap; }
  .review-item .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  .review-item input { flex:1; min-width:140px; background:#161b22; border:1px solid #30363d; border-radius:6px; color:#e6edf3; padding:4px 8px; font:inherit; font-size:12px; }
  .prop { border:1px solid #21262d; border-radius:8px; padding:10px; margin:8px 0; background:#0d1117; font-size:12px; }
  .prop .t { color:#ffa657; font-weight:bold; margin-bottom:4px; }
  .prop .b { color:#e6edf3; margin-bottom:8px; white-space:pre-wrap; }
  .cap-row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; border-bottom:1px solid #21262d; padding:8px 0; font-size:12px; }
  .cap-row .n { color:#7ee787; min-width:120px; }
  .cap-row .d { color:#8b949e; flex:1; min-width:200px; }
  .cap-row input { min-width:120px; background:#0d1117; border:1px solid #30363d; border-radius:6px; color:#e6edf3; padding:3px 6px; font:inherit; font-size:12px; }
  ul.todo li .x, ul.goals li .x { color:#6e7681; }
  #rv-journals a { color:#79c0ff; }
</style>
</head>
<body>
<header>
  <h1>🧠 BRAIN MONITOR</h1>
  <div class="pill">state <b id="fsm">-</b></div>
  <div class="pill">mode <b id="mode">-</b> / eff <b id="emode">-</b></div>
  <div class="pill">action <b id="action">-</b></div>
  <div class="pill">idle <b id="idle">-</b>s</div>
  <div class="pill">refractory <b id="refr">-</b>s</div>
  <div class="pill">fires <b id="fires">-</b></div>
  <div class="pill">mood <b id="mood2">-</b></div>
</header>
<main>
  <div class="card"><h2>Drives 内驱</h2><div id="drives"></div></div>
  <div class="card"><h2>Signals 感知信号</h2><div id="signals"></div></div>
  <div class="card full"><h2>Drives over time</h2>
    <canvas id="chart" width="900" height="200"></canvas>
    <div class="legend">
      <span style="color:#d2a8ff">expression_urge</span>
      <span style="color:#7ee787">arousal</span>
      <span style="color:#79c0ff">social_need</span>
      <span style="color:#f85149">threshold</span>
    </div>
  </div>
  <div class="card full"><h2>Memory 记忆 / 历史</h2>
    <div class="kv"><span class="k">working window</span> <b id="histlen">-</b> turns　<span class="k">turn counter</span> <b id="turncnt">-</b></div>
    <h3 class="sub">对用户的印象</h3><div class="mem-sum" id="impression">-</div>
    <h3 class="sub">长期目标</h3><ul class="goals" id="goals"></ul>
    <h3 class="sub">待办清单</h3><ul class="todo" id="todos"></ul>
    <h3 class="sub">近期回顾 (温区 · 近期线性记忆)</h3><div class="mem-sum" id="digest">-</div>
    <h3 class="sub">长期记忆 / 向量库检索 (L2b)　<span class="k">库条数</span> <b id="veccount">-</b></h3>
    <div class="kv"><span class="k">query</span> <span id="vecquery">-</span></div>
    <div class="turns" id="vechits"></div>
    <h3 class="sub">工作窗口最近对话</h3><div class="turns" id="turns"></div>
  </div>
  <div class="card full"><h2>Control 控制</h2>
    <div class="ctl-row">
      <span class="k">主动模式</span>
      <button class="btn" data-mode="focus">安静</button>
      <button class="btn" data-mode="normal">普通</button>
      <button class="btn" data-mode="companion">陪聊</button>
      <button class="btn" data-mode="auto">自动</button>
      <span class="pill">当前 <b id="ctl-mode">-</b></span>
      <button class="btn" id="ctl-mic">麦克风 -</button>
      <button class="btn danger" id="ctl-stop">停止说话</button>
    </div>
    <div class="ctl-grid">
      <div>
        <h3 class="sub">待办清单（点方框完成 / × 删除）</h3>
        <ul class="todo" id="ctl-todos"></ul>
        <div class="ctl-row">
          <input id="ctl-todo-input" placeholder="新增待办，回车提交"/>
          <button class="btn" id="ctl-todo-add">添加</button>
        </div>
      </div>
      <div>
        <h3 class="sub">长期目标（× 删除）</h3>
        <ul class="goals" id="ctl-goals"></ul>
        <div class="ctl-row">
          <input id="ctl-goal-input" placeholder="新增目标，回车提交"/>
          <button class="btn" id="ctl-goal-add">添加</button>
        </div>
      </div>
    </div>
    <h3 class="sub">和她说话</h3>
    <div class="ctl-row">
      <input id="ctl-ask-input" placeholder="发一句话给她：当作你说的，会得到完整回复并朗读"/>
      <button class="btn" id="ctl-ask">发送</button>
      <button class="btn" id="ctl-say">只念这句</button>
    </div>
    <div class="msg" id="ctl-msg"></div>
  </div>
  <div class="card full"><h2>Review 打分 · 日报</h2>
    <div class="ctl-row">
      <button class="btn" id="rv-sample">抽 5 句打分</button>
      <button class="btn" id="rv-propose">生成修改意见</button>
      <button class="btn" id="rv-journal">生成今天的日报</button>
      <span class="pill">今日 token <b id="tk-total">-</b>（<b id="tk-p">-</b> + <b id="tk-c">-</b>）</span>
      <span class="pill">评分 <b id="rv-count">-</b>　均分 <b id="rv-avg">-</b></span>
      <span class="pill">待打分 <b id="rv-remaining">-</b></span>
    </div>
    <div id="rv-list"></div>
    <h3 class="sub">修改意见（通过的会写入"近期调整约定"，下一轮立刻生效）</h3>
    <div id="rv-proposals"></div>
    <h3 class="sub">最近日报</h3>
    <div id="rv-journals" class="kv"></div>
    <div class="msg" id="rv-msg"></div>
  </div>
  <div class="card full"><h2>Capabilities 本地能力</h2>
    <div class="ctl-row">
      <span class="k">目录 <code>capabilities/*.py</code>　·　新增文件自动发现，可在这里开关与试跑</span>
      <button class="btn" id="cap-refresh">刷新</button>
      <span class="msg" id="cap-msg"></span>
    </div>
    <div id="cap-list"></div>
  </div>
  <div class="card full"><h2>Logs 日志</h2>
    <div class="ctl-row">
      <select id="log-cat">
        <option value="">全部分类</option>
        <option>VAD</option><option>INPUT</option><option>BOT</option><option>BRAIN</option>
        <option>MEMORY</option><option>LLM</option><option>ASR</option><option>TTS</option>
        <option>CONTROL</option><option>CAP</option><option>JOURNAL</option><option>TELEMETRY</option>
        <option>UI</option><option>CONFIG</option><option>LIVE</option><option>OTHER</option>
      </select>
      <select id="log-level">
        <option value="">全部级别</option>
        <option value="DEBUG">DEBUG 及以上</option>
        <option value="INFO" selected>INFO 及以上</option>
        <option value="WARNING">WARNING 及以上</option>
        <option value="ERROR">ERROR 及以上</option>
      </select>
      <input id="log-q" placeholder="搜索日志（正文 / 模块）…"/>
      <span class="pill">显示 <b id="log-count">-</b> 条</span>
    </div>
    <div class="logs" id="logs"></div>
  </div>
</main>
<script>
const COLORS = {expression_urge:"#d2a8ff", arousal:"#7ee787", social_need:"#79c0ff"};
function bar(name, val, online, threshold){
  const pct = Math.round((val||0)*100);
  const thr = threshold!=null ? `<div class="thr" style="left:${Math.min(100,threshold*100)}%"></div>` : "";
  const color = COLORS[name] || "#58a6ff";
  return `<div class="bar ${online===false?'offline':''}">
    <div class="lab"><span>${name}</span><span>${(val||0).toFixed(3)}</span></div>
    <div class="track"><div class="fill" style="width:${pct}%;background:${color}"></div>${thr}</div>
  </div>`;
}
async function tick(){
  try{
    const s = await (await fetch('/brain/state')).json();
    if(!s || !s.drives){ return; }
    const fsm=document.getElementById('fsm'); fsm.textContent=s.fsm_state; fsm.className='state-'+s.fsm_state;
    document.getElementById('mode').textContent=s.mode;
    document.getElementById('emode').textContent=s.effective_mode;
    const act=document.getElementById('action'); act.textContent=s.action; act.className='act-'+s.action;
    document.getElementById('idle').textContent=s.idle_seconds;
    document.getElementById('refr').textContent=s.refractory_remaining;
    document.getElementById('fires').textContent=s.consecutive_fires;
    document.getElementById('mood2').textContent=s.mood||'-';
    const thr=s.fire_threshold;
    document.getElementById('drives').innerHTML =
      bar('expression_urge', s.drives.expression_urge, true, thr) +
      bar('arousal', s.drives.arousal, true) +
      bar('social_need', s.drives.social_need, true);
    const sig=s.signals||{}, on=s.signal_online||{};
    document.getElementById('signals').innerHTML =
      Object.keys(sig).map(k=>bar(k, sig[k], on[k])).join('');
    drawChart(thr);
  }catch(e){ /* server may not be ready */ }
}
async function drawChart(threshold){
  const h = await (await fetch('/brain/history')).json();
  const c = document.getElementById('chart'), ctx = c.getContext('2d');
  const W=c.width, H=c.height; ctx.clearRect(0,0,W,H);
  ctx.strokeStyle="#21262d"; ctx.beginPath();
  for(let i=0;i<=4;i++){const y=H*i/4; ctx.moveTo(0,y); ctx.lineTo(W,y);} ctx.stroke();
  if(threshold!=null){ ctx.strokeStyle="#f85149"; ctx.setLineDash([4,4]); ctx.beginPath();
    const y=H-threshold*H; ctx.moveTo(0,y); ctx.lineTo(W,y); ctx.stroke(); ctx.setLineDash([]); }
  if(!h.length) return;
  const keys=["expression_urge","arousal","social_need"];
  keys.forEach(k=>{
    ctx.strokeStyle=COLORS[k]; ctx.lineWidth=1.5; ctx.beginPath();
    h.forEach((p,i)=>{ const x=W*i/(h.length-1||1); const y=H-(p[k]||0)*H;
      i?ctx.lineTo(x,y):ctx.moveTo(x,y); }); ctx.stroke();
  });
}
function esc(s){ return (s==null?'':String(s)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
async function tickMemory(){
  try{
    const m = await (await fetch('/brain/memory')).json();
    if(!m) return;
    document.getElementById('histlen').textContent = (m.history_len!=null)?m.history_len:'-';
    document.getElementById('turncnt').textContent = (m.turn_counter!=null)?m.turn_counter:'-';
    document.getElementById('impression').textContent = m.user_impression || '（暂无）';
    document.getElementById('goals').innerHTML = (m.goals&&m.goals.length)
      ? m.goals.map(g=>`<li>${esc(g)}</li>`).join('') : '<li class="k">（空）</li>';
    document.getElementById('todos').innerHTML = (m.todolist&&m.todolist.length)
      ? m.todolist.map(t=>`<li class="${t.done?'done':''}">${t.done?'☑':'☐'} ${esc(t.text)}</li>`).join('')
      : '<li class="k">（空）</li>';
    document.getElementById('digest').textContent = m.recent_digest || '（暂无）';
    document.getElementById('veccount').textContent = (m.vec_count!=null && m.vec_count>=0)?m.vec_count:'-';
    document.getElementById('vecquery').textContent = m.vec_query || '（暂无）';
    document.getElementById('vechits').innerHTML = (m.vec_hits&&m.vec_hits.length)
      ? m.vec_hits.map(h=>`<div class="turn"><span class="ts">dist ${h.distance!=null?Number(h.distance).toFixed(3):'?'}</span> ${esc(h.text)}</div>`).join('')
      : '<div class="k">（本轮无命中）</div>';
    document.getElementById('turns').innerHTML = (m.working_window||[]).map(t=>
      `<div class="turn role-${esc(t.role)}"><span class="ts">${t.ts?esc(t.ts):''}</span><b>${esc(t.role)}</b>: ${esc(t.content)}</div>`).join('');
  }catch(e){}
}
async function tickLogs(){
  try{
    const p = new URLSearchParams();
    const cat = document.getElementById('log-cat').value;
    const lvl = document.getElementById('log-level').value;
    const q = document.getElementById('log-q').value.trim();
    if(cat) p.set('category', cat);
    if(lvl) p.set('level', lvl);
    if(q) p.set('q', q);
    p.set('limit', '500');
    const rows = await (await fetch('/brain/logs?' + p.toString())).json();
    const box = document.getElementById('logs');
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    box.innerHTML = rows.map(r=>
      `<div class="row"><span class="t">${esc(r.time)}</span> <span class="lv-${esc(r.level)}">${esc(r.level)}</span> ` +
      `<span style="color:#6e7681">[${esc(r.category||'OTHER')}]</span> ${esc(r.message)}</div>`).join('');
    if(atBottom) box.scrollTop = box.scrollHeight;
    document.getElementById('log-count').textContent = rows.length;
  }catch(e){}
}
// ---------------- Control 控制面 ----------------
let ctlState = {mic_on:false};
function ctlMsg(text, ok){
  const el = document.getElementById('ctl-msg');
  el.textContent = text; el.style.color = ok ? '#7ee787' : '#f85149';
}
async function ctl(action, payload){
  try{
    const r = await (await fetch('/control/'+action, {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload||{})
    })).json();
    if(r.ok){ ctlMsg('✔ ' + action + ' 完成', true); }
    else { ctlMsg('✘ ' + action + ' 失败：' + (r.error||''), false); }
    await tickControl();
    return r;
  }catch(e){ ctlMsg('✘ 请求失败：' + e, false); }
}
async function tickControl(){
  try{
    const r = await (await fetch('/control/state')).json();
    if(!r || !r.ok) return;
    const s = r.result || {};
    ctlState = s;
    document.getElementById('ctl-mode').textContent = s.mode || '-';
    document.querySelectorAll('[data-mode]').forEach(b=>{
      b.classList.toggle('active', b.dataset.mode === s.mode);
    });
    const mic = document.getElementById('ctl-mic');
    mic.textContent = s.mic_on ? '麦克风 开' : '麦克风 关';
    mic.classList.toggle('on', !!s.mic_on);
    document.getElementById('ctl-todos').innerHTML = (s.todolist||[]).length
      ? s.todolist.map(t=>`<li class="${t.done?'done':''}">`+
          `<a href="#" data-act="todo.${t.done?'undone':'done'}" data-text="${esc(t.text)}">${t.done?'☑':'☐'}</a> `+
          `${esc(t.text)} <a href="#" class="del" data-act="todo.remove" data-text="${esc(t.text)}">×</a></li>`).join('')
      : '<li class="k">（空）</li>';
    document.getElementById('ctl-goals').innerHTML = (s.goals||[]).length
      ? s.goals.map(g=>`<li>${esc(g)} <a href="#" class="del" data-act="goal.remove" data-text="${esc(g)}">×</a></li>`).join('')
      : '<li class="k">（空）</li>';
  }catch(e){}
}
function ctlInit(){
  document.querySelectorAll('[data-mode]').forEach(b=>
    b.addEventListener('click', ()=>ctl('mode.set', {mode:b.dataset.mode})));
  document.getElementById('ctl-mic').addEventListener('click', ()=>ctl('mic.set', {on: !ctlState.mic_on}));
  document.getElementById('ctl-stop').addEventListener('click', ()=>ctl('stop', {}));
  const addTodo = ()=>{ const el=document.getElementById('ctl-todo-input'); const v=el.value.trim();
    if(v) ctl('todo.add', {text:v}).then(()=>{ el.value=''; }); };
  const addGoal = ()=>{ const el=document.getElementById('ctl-goal-input'); const v=el.value.trim();
    if(v) ctl('goal.add', {text:v}).then(()=>{ el.value=''; }); };
  document.getElementById('ctl-todo-add').addEventListener('click', addTodo);
  document.getElementById('ctl-todo-input').addEventListener('keydown', e=>{ if(e.key==='Enter') addTodo(); });
  document.getElementById('ctl-goal-add').addEventListener('click', addGoal);
  document.getElementById('ctl-goal-input').addEventListener('keydown', e=>{ if(e.key==='Enter') addGoal(); });
  const ask = ()=>{ const el=document.getElementById('ctl-ask-input'); const v=el.value.trim();
    if(v) ctl('ask', {text:v}).then(()=>{ el.value=''; }); };
  document.getElementById('ctl-ask').addEventListener('click', ask);
  document.getElementById('ctl-ask-input').addEventListener('keydown', e=>{ if(e.key==='Enter') ask(); });
  document.getElementById('ctl-say').addEventListener('click', ()=>{
    const el=document.getElementById('ctl-ask-input'); const v=el.value.trim();
    if(v) ctl('speak', {text:v}); });
  const listClick = e=>{
    const el = e.target.closest('[data-act]'); if(!el) return;
    e.preventDefault();
    ctl(el.dataset.act, {text: el.dataset.text});
  };
  document.getElementById('ctl-todos').addEventListener('click', listClick);
  document.getElementById('ctl-goals').addEventListener('click', listClick);
}
// ---------------- Review / 日报 / 能力 ----------------
async function api(action, payload){
  const opt = {method: payload ? 'POST' : 'GET', headers:{'Content-Type':'application/json'}};
  if(payload) opt.body = JSON.stringify(payload);
  const r = await (await fetch('/api/' + action, opt)).json();
  if(!r.ok) throw new Error(r.error || 'failed');
  return r.result;
}
function rvMsg(text, ok){
  const el = document.getElementById('rv-msg');
  el.textContent = text; el.style.color = ok ? '#7ee787' : '#f85149';
}
function rvItem(t){
  return `<div class="review-item" data-id="${t.id}">
    <div class="q">你：${esc(t.user_text)}</div>
    <div class="a">她：${esc(t.reply)}</div>
    <div class="row">
      <select class="rv-score">${[1,2,3,4,5].map(n=>`<option value="${n}"${n===4?' selected':''}>${n} 分</option>`).join('')}</select>
      <input class="rv-tags" placeholder="标签（可空）：太长 / 太冷 / 出戏 / 很好"/>
      <input class="rv-comment" placeholder="备注（可空）"/>
      <button class="btn rv-save">保存打分</button>
    </div>
  </div>`;
}
async function rvSample(){
  try{
    const r = await api('review.sample', {n:5});
    document.getElementById('rv-list').innerHTML =
      (r.turns||[]).length ? r.turns.map(rvItem).join('') : '<div class="k">没有待打分的句子了（或最近没有对话）。</div>';
    document.getElementById('rv-remaining').textContent = (r.remaining!=null) ? r.remaining : '-';
  }catch(e){ rvMsg('抽样失败：'+e.message, false); }
}
async function rvStats(){
  try{
    const r = await api('tokens.stats', {});
    const t = r.tokens||{}, st = r.review_stats||{};
    document.getElementById('tk-total').textContent = (t.total_tokens!=null) ? t.total_tokens : '-';
    document.getElementById('tk-p').textContent = (t.prompt_tokens!=null) ? t.prompt_tokens : '-';
    document.getElementById('tk-c').textContent = (t.completion_tokens!=null) ? t.completion_tokens : '-';
    document.getElementById('rv-count').textContent = (st.count!=null) ? st.count : '-';
    document.getElementById('rv-avg').textContent = (st.avg!=null) ? st.avg : '-';
  }catch(e){}
}
async function rvLoadProposals(){
  try{
    const r = await api('proposal.list', {status:'pending'});
    const list = r.proposals || [];
    document.getElementById('rv-proposals').innerHTML = list.length ? list.map(p=>`
      <div class="prop" data-pid="${p.id}">
        <div class="t">${esc(p.title)}</div>
        <div class="b">${esc(p.body)}</div>
        <button class="btn p-accept" data-id="${p.id}">通过</button>
        <button class="btn p-reject" data-id="${p.id}">忽略</button>
      </div>`).join('') : '<div class="k">暂无待处理的修改意见。</div>';
  }catch(e){}
}
async function rvLoadJournals(){
  try{
    const r = await api('journal.list', {limit:7});
    const list = r.journals || [];
    document.getElementById('rv-journals').innerHTML = list.length
      ? list.map(j=>`<a href="/journal/${esc(j.day)}" target="_blank">${esc(j.day)}</a>`).join('　·　')
      : '（还没有日报；点上面的「生成今天的日报」试试）';
  }catch(e){}
}
async function rvGenerateProposals(){
  rvMsg('正在根据评分生成修改意见…', true);
  try{ await api('review.generate_proposals', {}); rvMsg('已生成，见下方列表。', true); await rvLoadProposals(); }
  catch(e){ rvMsg('生成失败：'+e.message, false); }
}
async function rvWriteJournal(){
  try{ const r = await api('journal.write', {}); rvMsg('日报已生成：'+r.path, true); await rvLoadJournals(); }
  catch(e){ rvMsg('生成日报失败：'+e.message, false); }
}
document.getElementById('rv-sample').addEventListener('click', rvSample);
document.getElementById('rv-propose').addEventListener('click', rvGenerateProposals);
document.getElementById('rv-journal').addEventListener('click', rvWriteJournal);
document.getElementById('rv-list').addEventListener('click', async e=>{
  const btn = e.target.closest('.rv-save'); if(!btn) return;
  const item = btn.closest('.review-item');
  const payload = {
    turn_id: Number(item.dataset.id),
    score: Number(item.querySelector('.rv-score').value),
    tags: item.querySelector('.rv-tags').value.trim(),
    comment: item.querySelector('.rv-comment').value.trim(),
  };
  try{
    await api('review.submit', payload);
    item.remove();
    rvMsg('已记录。', true);
    await rvStats();
  }catch(err){ rvMsg('保存失败：'+err.message, false); }
});
document.getElementById('rv-proposals').addEventListener('click', async e=>{
  const b = e.target.closest('.p-accept,.p-reject'); if(!b) return;
  const approve = b.classList.contains('p-accept');
  try{
    await api('proposal.decide', {id:Number(b.dataset.id), approve});
    rvMsg(approve ? '已通过，写入「近期调整约定」。' : '已忽略。', true);
    await rvLoadProposals();
  }catch(err){ rvMsg('操作失败：'+err.message, false); }
});

async function capLoad(){
  try{
    const r = await api('capability.list', {});
    if(!r.enabled){ document.getElementById('cap-list').innerHTML = '<div class="k">能力系统已在配置里关闭。</div>'; return; }
    const caps = r.capabilities || [];
    document.getElementById('cap-list').innerHTML = caps.length ? caps.map(c=>`
      <div class="cap-row" data-name="${esc(c.name)}">
        <span class="n">${esc(c.name)}</span>
        <span class="d">${esc(c.description)}</span>
        <span class="k">${c.runs} 次 · 平均 ${c.avg_ms}ms</span>
        <input class="cap-args" value='{}' title='运行参数 JSON'/>
        <button class="btn cap-run">运行</button>
        <button class="btn cap-toggle">${c.enabled?'停用':'启用'}</button>
      </div>`).join('') : '<div class="k">capabilities 目录下还没有 .py 能力。</div>';
  }catch(e){ document.getElementById('cap-msg').textContent = '加载能力失败：'+e.message; }
}
document.getElementById('cap-refresh').addEventListener('click', capLoad);
document.getElementById('cap-list').addEventListener('click', async e=>{
  const row = e.target.closest('.cap-row'); if(!row) return;
  const name = row.dataset.name;
  try{
    if(e.target.closest('.cap-toggle')){
      const turningOff = e.target.textContent === '停用';
      await api('capability.toggle', {name, enabled: !turningOff});
      await capLoad();
    }else if(e.target.closest('.cap-run')){
      const args = row.querySelector('.cap-args').value || '{}';
      const r = await api('capability.run', {name, args});
      document.getElementById('cap-msg').textContent = `${name} → ${r.result}`;
      document.getElementById('cap-msg').style.color = '#7ee787';
      await capLoad();
    }
  }catch(err){
    document.getElementById('cap-msg').textContent = '操作失败：'+err.message;
    document.getElementById('cap-msg').style.color = '#f85149';
  }
});
let _logTimer = null;
document.getElementById('log-q').addEventListener('input', ()=>{
  clearTimeout(_logTimer); _logTimer = setTimeout(tickLogs, 300);
});
document.getElementById('log-cat').addEventListener('change', tickLogs);
document.getElementById('log-level').addEventListener('change', tickLogs);

capLoad(); rvStats(); rvLoadProposals(); rvLoadJournals();

ctlInit();
setInterval(tickControl, 2000); tickControl();
setInterval(tick, 300); tick();
setInterval(tickMemory, 1500); tickMemory();
setInterval(tickLogs, 1500); tickLogs();
</script>
</body>
</html>"""
