@echo off
chcp 65001 >nul
setlocal

REM ============================================================
REM  ZerolanLiveRobot 一键启动脚本
REM  拉起两个服务，各占一个独立控制台窗口：
REM    1) GPT-SoVITS TTS API (:9880)        —— 语音合成（同端口也提供 ASR）
REM    2) ZerolanLiveRobot   (main.py)      —— 主程序 / 大脑
REM  长期记忆向量库已内嵌进 robot（ChromaDB，无需单独进程/不需要 zerolan-core）。
REM  日志与历史/记忆会汇总到主程序的 Brain WebUI：
REM    http://127.0.0.1:8899/brain
REM ============================================================

REM ---------- 可编辑配置（按你的环境改这里）----------
set "ROBOT_DIR=c:\Work\ai\ZerolanLiveRobot"
set "SOVITS_DIR=c:\Work\ai\GPT-SoVITS"

REM 各服务先“激活”各自的 venv（让 PATH/ffmpeg/CUDA 等环境变量就绪），再用激活后的 python 跑
set "ROBOT_VENV_ACTIVATE=%ROBOT_DIR%\ZRExistence_venv\Scripts\activate.bat"
set "SOVITS_VENV_ACTIVATE=%SOVITS_DIR%\GPT-SoVITS-venv\Scripts\activate.bat"

REM GPT-SoVITS 启动参数
set "SOVITS_HOST=127.0.0.1"
set "SOVITS_PORT=9880"
set "SOVITS_CONFIG=GPT_SoVITS/configs/tts_infer.yaml"

REM 拉起 TTS 后、再启动 robot 前的等待秒数（给模型加载留时间）
set "WAIT_BEFORE_ROBOT=15"

REM Brain 监控面板（日志 + 历史/记忆）。启动 robot 后自动用浏览器打开；
REM 端口对应 config.yaml 里 service.res_server.port（默认 8899）
set "BRAIN_URL=http://127.0.0.1:8899/brain"
REM 等 robot 起好资源服务器、再打开面板的秒数
set "WAIT_BEFORE_PANEL=10"
REM --------------------------------------------------

echo [1/2] 启动 GPT-SoVITS TTS API (:%SOVITS_PORT%)...
start "GPT-SoVITS-TTS" cmd /k "cd /d "%SOVITS_DIR%" && call "%SOVITS_VENV_ACTIVATE%" && python api_v2.py -a %SOVITS_HOST% -p %SOVITS_PORT% -c %SOVITS_CONFIG%"

echo 等待 %WAIT_BEFORE_ROBOT% 秒，让 TTS 先就绪...
timeout /t %WAIT_BEFORE_ROBOT% /nobreak >nul

echo [2/2] 启动 ZerolanLiveRobot 主程序...
start "ZerolanLiveRobot" cmd /k "cd /d "%ROBOT_DIR%" && call "%ROBOT_VENV_ACTIVATE%" && python main.py"

echo 等待 %WAIT_BEFORE_PANEL% 秒，待资源服务器就绪后打开 Brain 监控面板...
timeout /t %WAIT_BEFORE_PANEL% /nobreak >nul
start "" "%BRAIN_URL%"

echo.
echo 两个服务已在各自窗口启动。
echo Brain WebUI（日志 + 历史/记忆）: %BRAIN_URL%
echo 关闭对应控制台窗口即可停止该服务。
echo.
endlocal
