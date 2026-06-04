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

        # 大脑面板每 ~300ms 轮询一次 /brain/state 与 /brain/history，werkzeug 开发服务器默认
        # 会为每个请求打一条访问日志，导致刷屏。这里把 werkzeug 访问日志降到 WARNING 级，
        # 只保留真正的告警/错误（不影响本项目自身的 loguru 日志）。
        logging.getLogger("werkzeug").setLevel(logging.WARNING)

        @self.app.route("/brain/state")
        def brain_state():
            return jsonify(monitor.get_latest() or {})

        @self.app.route("/brain/history")
        def brain_history():
            return jsonify(monitor.get_history())

        @self.app.route("/brain")
        def brain_dashboard():
            return Response(_BRAIN_DASHBOARD_HTML, mimetype="text/html")

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
setInterval(tick, 300); tick();
</script>
</body>
</html>"""
