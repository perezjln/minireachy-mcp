"""A simulated MiniReachy daemon served over real HTTP on 127.0.0.1.

Canned responses are copies of answers captured from a real daemon (v1.8.0).
Every request is recorded so tests can assert that nothing was sent.
"""

from __future__ import annotations

import asyncio
import copy
import json
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

OPENAPI = json.loads((Path(__file__).resolve().parents[1] / "docs" / "api" / "openapi.json").read_text("utf-8"))

DAEMON_STATUS = {
    "type": "daemon_status", "robot_name": "reachy_mini", "state": "running", "wireless_version": False,
    "desktop_app_daemon": True, "simulation_enabled": False, "mockup_sim_enabled": False, "no_media": False,
    "media_released": False, "camera_specs_name": "lite",
    "backend_status": {"ready": False, "motor_control_mode": "enabled", "last_alive": None,
                       "control_loop_stats": {"mean_control_loop_frequency": 36.9, "nb_error": 0}, "error": None},
    "error": None, "wlan_ip": None, "version": "1.8.0", "hardware_id": None,
}
HEAD_POSE = {"x": -0.0027, "y": -0.00004, "z": -0.0006, "roll": 0.0137, "pitch": 0.0262, "yaw": -0.0176}
VOLUME = {"volume": 100, "platform": "Windows", "device": "Echo Cancelling Speakerphone (Reachy Mini Audio)"}
APP = {"name": "reachy_mini_conversation_app", "source_kind": "installed", "description": "", "url": None,
       "extra": {"id": "pollen-robotics/reachy_mini_conversation_app",
                 "cardData": {"short_description": "Talk with Reachy Mini!"}}}

STATIC: dict[str, Any] = {
    "GET /api/daemon/status": DAEMON_STATUS,
    "GET /api/daemon/hardware-id": {"hardware_id": None},
    "GET /api/daemon/robot-app-lock-status": {"state": "free", "holder_name": None},
    "GET /api/motors/status": {"mode": "enabled"},
    "GET /api/state/full": {"control_mode": "enabled", "head_pose": HEAD_POSE, "head_joints": None,
                            "body_yaw": -0.0015, "antennas_position": [-0.2178, 0.0184],
                            "timestamp": "2026-10-11T17:16:41.203467Z", "passive_joints": None,
                            "doa": {"angle": 2.2165, "speech_detected": False}},
    "GET /api/state/present_head_pose": HEAD_POSE,
    "GET /api/state/present_body_yaw": -0.0015339807878858025,
    "GET /api/state/present_antenna_joint_positions": [-0.2178, 0.0184],
    "GET /api/state/doa": {"angle": 2.2165, "speech_detected": False},
    "GET /api/media/status": {"available": True, "released": False, "no_media": False},
    "GET /api/media/sounds": {"files": []},
    "GET /api/volume/current": VOLUME,
    "GET /api/volume/microphone/current": VOLUME,
    "GET /api/camera/specs": {"name": "lite", "available_resolutions": [], "default_resolution":
                              {"name": "R1920x1080at60fps", "width": 1920, "height": 1080, "fps": 60,
                               "crop_factor": 1.115}, "K": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "D": [0.0]},
    "GET /api/kinematics/info": {"info": {"engine": "AnalyticalKinematics", "collision check": False}},
    "GET /api/kinematics/urdf": {"urdf": "<?xml version=\"1.0\" ?>" + "<robot>" + "x" * 50_000 + "</robot>"},
    "GET /api/apps/current-app-status": None,
    "GET /api/apps/check-updates": {"apps_with_updates": [], "apps_checked": 1, "apps_skipped": 0},
    "GET /api/hf-auth/status": {"is_logged_in": True, "username": "someone"},
    "GET /api/hf-auth/relay-status": {"state": "unavailable", "message": "Coming soon to Lite version",
                                      "is_connected": False},
    "GET /api/hf-auth/oauth/configured": {"configured": True},
    "GET /api/hf-auth/central-robot-status": {"available": False, "robots": [], "reason": "test"},
    "GET /openapi.json": OPENAPI,
    "POST /api/move/play/wake_up": "MOVE",
    "POST /api/move/play/goto_sleep": "MOVE",
    "POST /api/move/goto": "MOVE",
    "POST /api/move/stop": {"message": "Move stopped"},
    "POST /api/media/play_sound": {"status": "playing"},
    "POST /api/media/stop_sound": {"status": "stopped"},
    "POST /api/media/wobbling/enable": {"status": "enabled"},
    "POST /api/media/wobbling/disable": {"status": "disabled"},
    "POST /api/media/release": {"status": "released"},
    "POST /api/media/acquire": {"status": "acquired"},
    "POST /api/volume/test-sound": {"status": "ok", "message": "played"},
    "POST /api/apps/stop-current-app": None,
    "POST /api/apps/restart-current-app": {"info": APP, "state": "starting", "error": None},
    "POST /api/apps/install": {"job_id": "11111111-2222-3333-4444-555555555555"},
    "POST /api/apps/install-private-space": {"job_id": "11111111-2222-3333-4444-555555555555"},
    "POST /api/hf-auth/refresh-relay": {"status": "skipped", "token_available": True, "reason": "relay_not_running"},
    "POST /api/daemon/start": {"status": "starting"},
    "POST /api/daemon/stop": {"status": "stopping"},
    "POST /api/daemon/restart": {"status": "restarting"},
    "POST /api/audio/config/apply": {"applied": True},
}


@dataclass
class Recorded:
    method: str
    path: str
    query: dict[str, str]
    body: Any
    raw_path: str
    at: float = field(default_factory=time.monotonic)  # arrival time


@dataclass
class Override:
    status: int = 200
    body: Any = None
    raw: bytes | None = None
    delay: float = 0.0
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)


class FakeDaemon:
    def __init__(self) -> None:
        self.requests: list[Recorded] = []
        self.overrides: dict[str, Override] = {}
        self.running_moves: list[str] = []
        self.motor_mode = "enabled"
        self.sounds: list[str] = []
        self.port = 0
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    # -- helpers for tests ------------------------------------------------------------
    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def calls(self, method: str | None = None) -> list[Recorded]:
        return [r for r in self.requests if method is None or r.method == method]

    def non_get_calls(self) -> list[Recorded]:
        return [r for r in self.requests if r.method != "GET"]

    def reset(self) -> None:
        self.requests.clear()
        self.overrides.clear()
        self.running_moves.clear()
        self.motor_mode = "enabled"
        self.sounds.clear()

    # -- ASGI -------------------------------------------------------------------------
    async def handle(self, request: Request) -> Response:
        path = request.url.path
        raw_path = request.scope.get("raw_path", b"").decode()
        body: Any = None
        raw = await request.body()
        if raw and request.headers.get("content-type", "").startswith("application/json"):
            body = json.loads(raw)
        elif raw:
            body = {"_multipart_bytes": len(raw), "_content_type": request.headers.get("content-type")}
        self.requests.append(Recorded(request.method, path, dict(request.query_params), body, raw_path))
        key = f"{request.method} {path}"

        ov = self.overrides.get(key)
        if ov is not None:
            if ov.delay:
                await asyncio.sleep(ov.delay)
            if ov.raw is not None:
                return Response(ov.raw, status_code=ov.status, media_type=ov.content_type, headers=ov.headers)
            return JSONResponse(ov.body, status_code=ov.status, headers=ov.headers)
        return self.default(request.method, path, body)

    def default(self, method: str, path: str, body: Any) -> Response:
        key = f"{method} {path}"
        if key == "GET /api/move/running":
            return JSONResponse([{"uuid": u} for u in self.running_moves])
        if key == "POST /api/move/stop":
            uid = (body or {}).get("uuid")
            if uid in self.running_moves:
                self.running_moves.remove(uid)
            return JSONResponse({"message": f"Stopped move with UUID: {uid}"})
        if method == "POST" and path.startswith("/api/motors/set_mode/"):
            mode = path.rsplit("/", 1)[1]
            if mode not in ("enabled", "disabled", "gravity_compensation"):
                return JSONResponse({"detail": [{"loc": ["path", "mode"], "msg": "Input should be ..."}]}, 422)
            self.motor_mode = mode
            return JSONResponse({"status": f"motors changed to {mode} mode"})
        if key == "GET /api/motors/status":
            return JSONResponse({"mode": self.motor_mode})
        if key == "POST /api/volume/set" or key == "POST /api/volume/microphone/set":
            return JSONResponse({**VOLUME, "volume": body["volume"]})
        if path.startswith("/api/move/recorded-move-datasets/list/"):
            return JSONResponse(["cheerful1", "dance1", "yes1"])
        if method == "POST" and path.startswith("/api/move/play/recorded-move-dataset/"):
            return self._new_move()
        if path.startswith("/api/apps/list-available"):
            kind = path.rsplit("/", 1)[1] if path.count("/") == 4 else None
            apps = [APP, {**APP, "name": "hand_tracker", "source_kind": "hf_space"}]
            return JSONResponse([a for a in apps if kind in (None, a["source_kind"])])
        if path.startswith("/api/apps/job-status/"):
            return JSONResponse({"command": "install", "status": "done", "logs": [f"line {i}" for i in range(80)]})
        if method == "POST" and path.startswith("/api/apps/start-app/"):
            return JSONResponse({"info": {**APP, "name": path.rsplit("/", 1)[1]}, "state": "starting", "error": None})
        if method == "POST" and (path.startswith("/api/apps/remove/") or path.startswith("/api/apps/update/")):
            return JSONResponse({"job_id": "11111111-2222-3333-4444-555555555555"})
        if path.startswith("/api/audio/config/parameter/"):
            name = path.rsplit("/", 1)[1]
            if name == "NOPE":
                return JSONResponse({"detail": f"Parameter '{name}' could not be read"}, 404)
            return JSONResponse({"name": name, "values": [1.0]})
        if key == "POST /api/media/sounds/upload":
            self.sounds.append("uploaded.wav")
            return JSONResponse({"path": "/tmp/reachy_mini_sounds/uploaded.wav"})
        if method == "DELETE" and path.startswith("/api/media/sounds/"):
            return JSONResponse({"status": "deleted"})
        if key in STATIC:
            value = STATIC[key]
            if value == "MOVE":
                return self._new_move()
            return JSONResponse(copy.deepcopy(value))
        return JSONResponse({"detail": "Not Found"}, 404)

    def _new_move(self) -> Response:
        uid = str(uuid.uuid4())
        self.running_moves.append(uid)
        return JSONResponse({"uuid": uid})

    def app(self) -> Starlette:
        methods = ["GET", "POST", "DELETE", "PUT", "PATCH"]
        return Starlette(routes=[Route("/{path:path}", self.handle, methods=methods)])

    # -- lifecycle (own thread + event loop, real TCP socket) -------------------------
    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        config = uvicorn.Config(self.app(), log_level="warning", lifespan="off")
        self._server = uvicorn.Server(config)
        self._server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [sock]}, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("fake daemon did not start")
            time.sleep(0.02)

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=10)
