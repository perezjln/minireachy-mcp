# minireachy-mcp

An **[MCP (Model Context Protocol)](https://modelcontextprotocol.io)** server that lets an AI agent (Claude Code, an IDE, any MCP client) control a **[Reachy Mini](https://github.com/pollen-robotics/reachy_mini)** robot ("MiniReachy", by [Pollen Robotics](https://www.pollen-robotics.com) and [Hugging Face](https://huggingface.co/pollen-robotics)) through its **native daemon**.

> **Unofficial project.** It is not affiliated with, endorsed by or supported by Pollen Robotics or Hugging Face. It talks to the public HTTP API and WebRTC stream of the Reachy Mini daemon.

## Highlights

* The MCP server runs **in Docker Compose**. The Reachy Mini daemon stays on the host machine and is never installed in the container.
* The transport is **Streamable HTTP** on `http://127.0.0.1:8765/mcp`, using the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) `mcp==2.3.0` (v2 branch).
* **68 tools** cover the 57 relevant operations of the daemon API (v1.8.0). 10 operations are excluded, each with a justification. See [docs/api-inventory.md](docs/api-inventory.md).
* **Perception and voice**: Reachy sees (camera), listens (microphones, local [faster-whisper](https://github.com/SYSTRAN/faster-whisper) speech recognition) and speaks (local [Piper](https://github.com/OHF-Voice/piper1-gpl) speech synthesis, French voice, or English with `language="en"`). Everything goes through the daemon's WebRTC stream, with no USB access from the container.
* **Conversational skill**: [.claude/skills/reachy-conversation](.claude/skills/reachy-conversation/SKILL.md) turns Reachy into a conversational agent driven by Claude.
* Physical actions, administration actions and perception (camera and microphones) are **refused by default**.

## Dependencies and useful links

| Component | Used for | Link |
|---|---|---|
| Reachy Mini (robot, SDK and daemon) | The robot and the daemon this server controls | [github.com/pollen-robotics/reachy_mini](https://github.com/pollen-robotics/reachy_mini) |
| Reachy Mini documentation | Setup of the robot and of the daemon / desktop app | [huggingface.co/docs/reachy_mini](https://huggingface.co/docs/reachy_mini) |
| Pollen Robotics | Maker of Reachy Mini | [pollen-robotics.com](https://www.pollen-robotics.com), [huggingface.co/pollen-robotics](https://huggingface.co/pollen-robotics) |
| Reachy Mini emotions library | Recorded moves played by `play_recorded_move` | [huggingface.co/datasets/pollen-robotics/reachy-mini-emotions-library](https://huggingface.co/datasets/pollen-robotics/reachy-mini-emotions-library) |
| Hugging Face Hub | Source of the speech models downloaded at image build time | [huggingface.co](https://huggingface.co) |
| `Systran/faster-whisper-small` | Speech-to-text model | [huggingface.co/Systran/faster-whisper-small](https://huggingface.co/Systran/faster-whisper-small) |
| `rhasspy/piper-voices` | Text-to-speech voices (French `siwis`, English `lessac`) | [huggingface.co/rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices) |
| faster-whisper | Local speech recognition engine | [github.com/SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper) |
| Piper (`piper-tts`) | Local speech synthesis engine | [github.com/OHF-Voice/piper1-gpl](https://github.com/OHF-Voice/piper1-gpl) |
| aiortc | WebRTC client for the daemon's camera / microphone stream | [github.com/aiortc/aiortc](https://github.com/aiortc/aiortc) |
| GStreamer `webrtcsink` | WebRTC media server run by the daemon | [github.com/GStreamer/gst-plugins-rs](https://github.com/GStreamer/gst-plugins-rs) |
| Model Context Protocol | The protocol and its Python SDK | [modelcontextprotocol.io](https://modelcontextprotocol.io), [github.com/modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk) |
| Docker Desktop / Docker Compose | Runtime of the MCP server | [docs.docker.com/compose](https://docs.docker.com/compose/) |

Python dependencies are pinned in [pyproject.toml](pyproject.toml) and in the hashed lock files `requirements.lock` / `requirements-test.lock`.

## Quick start

1. **Start the Reachy Mini daemon** on the host (the "Reachy Mini Control" desktop app, see the [Reachy Mini documentation](https://huggingface.co/docs/reachy_mini)) and check that the robot is connected. By default the daemon listens on port `8000`.
2. **Configure and start the MCP server** (requires Docker with Compose v2.24+):

   ```bash
   cp .env.example .env
   docker compose up --build -d
   ```

   The first build downloads the speech models and takes a few minutes.
3. **Check that it works**:

   ```bash
   curl http://127.0.0.1:8765/healthz          # the MCP server
   curl http://127.0.0.1:8765/healthz/daemon   # the MCP server can reach the Reachy Mini daemon
   ```
4. **Connect your MCP client** to `http://127.0.0.1:8765/mcp`. With Claude Code:

   ```bash
   claude mcp add --transport http minireachy http://127.0.0.1:8765/mcp
   ```

   Other clients: see [section 8](#8-configuring-an-mcp-client). Read-only tools (`get_daemon_status`, `get_full_state`…) work right away.
5. **Allow the robot to act.** Everything that moves the robot or uses its camera and microphones is refused by default. To enable it, edit `.env`, then restart:

   ```
   ENABLE_PHYSICAL_ACTIONS=true   # movements, sound, speaking
   ENABLE_PERCEPTION=true         # camera and microphones
   ```

   ```bash
   docker compose up -d
   ```

   `get_server_safety_config` tells you which switches are currently on.

### Using the MCP server

Ask your MCP client in natural language ("what is Reachy's state?", "nod your head", "take a picture"). The client picks the right tools; the full list is in [section 9](#9-available-tools). Every tool returns structured JSON, and a refused action comes back as `refused_by_safety_policy` with the switch to turn on.

### Using the conversation skill (Claude Code)

The skill [`reachy-conversation`](.claude/skills/reachy-conversation/SKILL.md) turns Reachy into a spoken conversational agent: Reachy listens, optionally looks through its camera, and answers aloud with gestures, with Claude as the brain.

1. Complete the quick start above, with **both** `ENABLE_PHYSICAL_ACTIONS=true` and `ENABLE_PERCEPTION=true`.
2. Open Claude Code **in this repository folder**. The project's `.mcp.json` declares the `minireachy` server and the skill is picked up from `.claude/skills/`. Approve the project MCP server when asked, and make sure it is connected (the server must be running *before* you start Claude Code, or reconnect it with `/mcp`).
3. Start the conversation with `/reachy-conversation`, or just say "talk with Reachy". Say "stop" to freeze the robot and "au revoir Reachy" (or "goodbye") to end.
4. Reachy speaks French by default. Switch to English by asking for it: the skill then passes `language="en"`.

To use the skill from another project, copy `.claude/skills/reachy-conversation` into that project's `.claude/skills/` (or into `~/.claude/skills/` for all your projects) and register the MCP server as shown in step 4 above. Expect about 4 to 10 s of latency per exchange. More detail in [Perception, voice and conversational agent](#perception-voice-and-conversational-agent).

---

## 1. Architecture

```
 MCP client (Claude Code, IDE, agent…)
        │  Streamable HTTP  http://127.0.0.1:8765/mcp   (published on the loopback interface only)
        ▼
┌──────────────── minireachy-mcp container (Docker Compose) ────────────────┐
│ server.py   MCPServer + /healthz + optional Bearer auth + DNS-rebinding guard │
│ tools/      tools by domain: daemon, state, motion, media, apps                │
│ safety.py   ENABLE_PHYSICAL_ACTIONS / ENABLE_ADMIN_ACTIONS gates + guardrails │
│ client.py   single httpx client: timeouts, max size, GET-only retries          │
└────────────────────────────────────────────────────────────────────────────┘
        │  HTTP  MINIREACHY_BASE_URL (default http://host.docker.internal:8000)
        ▼
 Reachy Mini daemon (native, on the host) ── USB ── robot
```

| File | Role |
|---|---|
| `src/minireachy_mcp/config.py` | Environment variables, validation, safe defaults |
| `src/minireachy_mcp/client.py` | Daemon HTTP client (timeouts, errors, response size, correlation) |
| `src/minireachy_mcp/errors.py` | Typed errors, written to be usable by an LLM |
| `src/minireachy_mcp/models.py` | Input models mirroring the OpenAPI schemas |
| `src/minireachy_mcp/safety.py` | Risk classes, safety gates, motion guardrails |
| `src/minireachy_mcp/tools/` | MCP tools by domain |
| `src/minireachy_mcp/inventory.py` | Excluded endpoints and their justification |
| `src/minireachy_mcp/diagnostics.py`, `diagnose.py` | 4-step network diagnostic, OpenAPI drift detection |
| `src/minireachy_mcp/server.py`, `__main__.py` | Server assembly, HTTP application, graceful shutdown |
| `tests/` | Unit tests, integration with a simulated daemon, MCP protocol, e2e |
| `docs/api/openapi.json` | Snapshot of the daemon's OpenAPI schema (test reference) |

## 2. Prerequisites

* Docker Engine with Docker Compose v2.24 or newer. Validated with Docker Desktop (Windows 11), Docker 29.8.2 and Compose v5.5.1.
* The Reachy Mini daemon ("Reachy Mini Control" application) running on the host. Validated with version 1.8.0, on a Reachy Mini Lite over USB.
* No Python is needed on the host: build, tests and diagnostics all go through Docker.

## 3. Configuration

```bash
cp .env.example .env
```

All variables are documented in [.env.example](.env.example). The main ones:

| Variable | Default | Role |
|---|---|---|
| `MINIREACHY_BASE_URL` | `http://host.docker.internal:8000` | Daemon URL **as seen from the container** |
| `MINIREACHY_CONNECT_TIMEOUT` / `MINIREACHY_READ_TIMEOUT` | `3` / `15` s | HTTP timeouts |
| `MINIREACHY_GET_RETRIES` | `1` | Retries, for reads (GET) only |
| `MINIREACHY_MAX_RESPONSE_BYTES` | `2000000` | Responses larger than this are rejected |
| `MCP_PUBLISH_PORT` | `8765` | Port published on the host's `127.0.0.1` |
| `MCP_PATH` | `/mcp` | MCP endpoint path |
| `MCP_AUTH_TOKEN` | empty | Bearer token (24 characters minimum). Empty = no authentication |
| `MCP_ALLOWED_HOSTS` | loopback + `minireachy-mcp:*` | Accepted `Host` headers (DNS-rebinding protection) |
| `ENABLE_PHYSICAL_ACTIONS` | `false` | Allows movements, motor torque, sound and app starts |
| `ENABLE_ADMIN_ACTIONS` | `false` | Allows daemon lifecycle, app installation, media release and audio DSP |
| `ENABLE_PERCEPTION` | `false` | Allows the camera (`capture_image`) and listening (`listen`) |
| `MEDIA_SIGNALLING_URL` | `ws://host.docker.internal:8443` | WebRTC signalling of the daemon's media server |
| `MEDIA_IDLE_DISCONNECT_S` | `300` | Closes the camera/microphone stream after this many seconds without use (`0` = never). It reopens on demand in about 3 s |
| `STT_LANGUAGE`, `STT_BEAM_SIZE`, `TTS_LENGTH_SCALE` | `fr`, `1`, `1.0` | Language and settings of speech recognition and synthesis |
| `TTS_VOICE_PATH_EN` | `en_US-lessac-medium` voice | English voice, used when `speak`/`converse` receive `language="en"`. Empty = disabled |
| `LIMIT_*` | see `.env.example` | `goto_pose` guardrails, specific to the MCP server |

No secret is present in the image or in the repository: `.env` is ignored by git and by the Docker build context.

## 4. Running

```bash
docker compose up --build -d
```

```bash
docker compose ps
```

```bash
docker compose logs -f minireachy-mcp
```

The `healthy` state applies to **the MCP server only**. If the daemon is stopped, the container stays healthy and does not restart in a loop. This behavior was verified: with an unreachable daemon, the container stays `healthy` with `RestartCount=0`. The tools then return `daemon_unreachable`.

To allow physical actions, the operator edits `.env`:

```
ENABLE_PHYSICAL_ACTIONS=true
```

then restarts:

```bash
docker compose up -d
```

## 5. Networking by host system

The daemon runs on the host and the MCP server in a container. The two points of view must therefore be distinguished:

| Point of view | Daemon | MCP server |
|---|---|---|
| Host | `http://localhost:8000` | `http://127.0.0.1:8765/mcp` |
| Container | `http://host.docker.internal:8000` | listens on `0.0.0.0:8765` (internal to the container) |
| Other Compose container | `http://host.docker.internal:8000` | `http://minireachy-mcp:8765/mcp` |

**Windows and macOS (Docker Desktop)**: `host.docker.internal` is provided by Docker Desktop. This case was verified on Windows: the name resolves to `192.168.65.254` and the daemon responds.

**Linux**: `compose.yaml` declares `extra_hosts: host.docker.internal:host-gateway`, which points this name to the host's gateway. Note that this is **not enough** if the daemon only listens on `127.0.0.1`: the Docker gateway cannot reach the host's loopback interface. Possible solutions, from the most to the least restrictive:

1. A TCP relay on the host, limited to the Docker bridge interface. For example:
   ```bash
   socat TCP-LISTEN:8000,bind=172.17.0.1,reuseaddr,fork TCP:127.0.0.1:8000
   ```
   You then need to check the real bridge IP (`ip addr show docker0`) and the firewall.
2. Make the daemon listen on the Docker bridge interface, with a firewall that only allows the Docker subnets.
3. `MINIREACHY_BASE_URL` pointing to another host IP, filtered by the firewall.

`network_mode: host` is deliberately not used. **The Linux case was not tested in this environment.**

**Security observation on the development host**: daemon v1.8.0 listens on `0.0.0.0:8000`, so on all interfaces, without authentication. It is reachable from the local network, independently of this project. Its configuration was not modified. It is advisable to restrict port 8000 with the host firewall (for example, on Windows, allow only `127.0.0.1` and the Docker Desktop subnet).

## 6. Checking connectivity to MiniReachy

From the container, four distinct steps are checked: DNS resolution, TCP port, HTTP response, then the functional state of the daemon.

```bash
docker compose exec minireachy-mcp python -m minireachy_mcp.diagnose
```

To add the comparison between the live OpenAPI and the tool inventory:

```bash
docker compose exec minireachy-mcp python -m minireachy_mcp.diagnose --drift
```

Health of the MCP server alone:

```bash
curl http://127.0.0.1:8765/healthz
```

Daemon reachability (answers 503 if the daemon is unreachable or not `running`):

```bash
curl http://127.0.0.1:8765/healthz/daemon
```

The same diagnostic exists as an MCP tool: `diagnose_connectivity`. A successful `daemon_status` step only means that the daemon reports the `running` state. It does not prove that the robot hardware is in good condition.

## 7. MCP address and transport

* Transport: **Streamable HTTP** (`mcp` SDK 2.3.0, stateful sessions, SSE responses).
* URL: `http://127.0.0.1:8765/mcp`, published **on the host's loopback interface only**.
* DNS-rebinding protection is active: only the `Host` headers listed in `MCP_ALLOWED_HOSTS` are accepted.
* Optional Bearer authentication with `MCP_AUTH_TOKEN`. The `/healthz*` routes do not need it.

**Access from another machine** (not configured by default):

1. Set `MCP_AUTH_TOKEN`.
2. Put a TLS reverse proxy (Caddy, Traefik, nginx) in front of the service.
3. Add the public host name to `MCP_ALLOWED_HOSTS`.
4. Restrict source addresses at the firewall.

Never publish `0.0.0.0:8765` without these protections. The token is static: there is no OAuth and no automatic rotation.

## 8. Configuring an MCP client

**Claude Code**:

```bash
claude mcp add --transport http minireachy http://127.0.0.1:8765/mcp
```

With a token:

```bash
claude mcp add --transport http minireachy http://127.0.0.1:8765/mcp --header "Authorization: Bearer YOUR_TOKEN"
```

**Generic client** (`.mcp.json` file, Cursor, VS Code…):

```json
{
  "mcpServers": {
    "minireachy": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer YOUR_TOKEN" }
    }
  }
}
```

Remove `headers` if `MCP_AUTH_TOKEN` is empty. The configuration commands of these clients were not run in this environment. Tool discovery and calls, however, were validated with the official SDK MCP client, over HTTP, from a container (`tests/test_e2e.py`).

## 9. Available tools

All tools return structured JSON (`structuredContent`). Errors are returned with `isError=true` and an explicit code: `refused_by_safety_policy`, `daemon_unreachable`, `uncertain_outcome`, `daemon_validation_error`, `daemon_http_error`, `invalid_response`, `response_too_large`. The endpoint-by-endpoint detail is in [docs/api-inventory.md](docs/api-inventory.md).

**Read and diagnostics (always available, no side effects)**:

| Domain | Tools |
|---|---|
| Daemon | `get_daemon_status`, `get_hardware_id`, `get_robot_app_lock_status`, `diagnose_connectivity`, `get_server_safety_config` |
| State | `get_full_state`, `get_head_pose`, `get_body_yaw`, `get_antenna_positions`, `get_direction_of_arrival`, `get_motor_status` |
| Moves | `list_running_moves`, `get_move_status`, `wait_for_move`, `list_recorded_moves` |
| Camera, kinematics | `get_camera_specs`, `get_kinematics_info`, `get_urdf` (truncated by default) |
| Media, audio | `get_media_status`, `list_uploaded_sounds`, `get_volume`, `get_microphone_volume`, `read_audio_parameter` |
| Apps | `list_apps` (compact form, filter), `get_current_app_status`, `check_app_updates`, `get_app_job_status` |
| Hugging Face | `get_hf_auth_status`, `get_relay_status`, `get_central_robot_status` |

**Stops (always allowed)**: `stop_move`, `stop_all_moves`, `stop_sound`, `stop_current_app`.

**Non-physical configuration (allowed)**: `set_microphone_volume`, `upload_sound` (base64, size-limited, audio extensions only), `delete_uploaded_sound`, `refresh_relay`.

**Physical (`ENABLE_PHYSICAL_ACTIONS=true`)**:

| Tool | Effect |
|---|---|
| `goto_pose` | Movement of the head, antennas and/or body, interpolated over `duration`. Subject to the guardrails |
| `wake_up` / `goto_sleep` | Built-in wake-up and sleep movements |
| `play_recorded_move` | Plays a recorded move (emotions, dances…) |
| `set_motor_mode` | `enabled` / `disabled` (no torque) / `gravity_compensation`, with a read-back to confirm |
| `play_sound`, `play_test_sound`, `set_volume` | Sound on the speaker. `set_volume` also plays a test sound |
| `enable_wobbling` / `disable_wobbling` | Head oscillations reacting to audio |
| `start_app`, `restart_current_app` | Apps drive the robot outside this server's guardrails |

**Administration (`ENABLE_ADMIN_ACTIONS=true`)**: `install_app`, `install_private_space`, `remove_app`, `update_app`, `release_media`, `acquire_media`, `apply_audio_config`.

**Physical + administration (both switches are required)**: `start_daemon`, `stop_daemon`, `restart_daemon`.

**Perception and voice**:

| Tool | Switch | Effect |
|---|---|---|
| `get_perception_status` | none (read) | State of the WebRTC stream (frames and audio received) and of the voice models. Does not open the stream |
| `capture_image` | `ENABLE_PERCEPTION` | JPEG photo from the head camera (what Reachy sees), resizable |
| `listen` | `ENABLE_PERCEPTION` | Waits for someone to speak, records until a pause and transcribes (local faster-whisper). Ignores noise and the tail of its own voice, and returns the direction of the voice |
| `converse` | `ENABLE_PERCEPTION` (+ `ENABLE_PHYSICAL_ACTIONS` to speak and move) | **One conversation turn in a single call**: says a sentence while moving (antenna gesture `salut`/`joie`/`curiosite`, head gesture `oui`/`non`, head turned toward a point of the last image with `look_at_point` or by a relative angle with `head_turn`), listens to the answer and transcribes it. With `look="auto"` (default), a photo is attached only if the answer asks to look ("regarde", "tu vois", "écrit", "see"…). Returns the measured timings |
| `look_at_point` | `ENABLE_PHYSICAL_ACTIONS` | Turns the head to bring the pixel (x, y) of the last returned image to the center. Computed from the camera intrinsics (`/api/camera/specs`) and the head pose stored when the photo was taken |
| `nudge_head` | `ENABLE_PHYSICAL_ACTIONS` | Turns the head by a small relative angle ("a bit to the right") and straightens it |
| `speak` | `ENABLE_PHYSICAL_ACTIONS` | Local speech synthesis (Piper), streamed over the WebRTC connection (fallback: upload then `play_sound`). Returns when playback is finished |
| `turn_toward_sound` | `ENABLE_PHYSICAL_ACTIONS` | Turns the head (yaw) toward the direction of the last detected voice |

## Perception, voice and conversational agent

### How it works

The daemon runs a GStreamer media server (`webrtcsink`) that broadcasts the camera and the microphones over WebRTC. Its signalling server runs on port `8443` of the host. This stream is **not** in the OpenAPI: it was identified in the daemon code (`reachy_mini/media/media_server.py`). It is the official channel used by the remote SDK and by browsers.

The MCP server connects to it as an ordinary client (aiortc library), on demand: at the first `capture_image` or `listen`, in about 3 s. It reconnects automatically if the daemon restarts. The container therefore never accesses USB directly. After `MEDIA_IDLE_DISCONNECT_S` (300 s by default) without any perception or voice call, the stream is closed and the buffered microphone audio is erased; the next call reopens it (`get_perception_status` then shows `idle_disconnected`).

```
mics/camera ─ daemon (host) ─ WebRTC :8443 ─▶ container: aiortc ─▶ JPEG image / 16 kHz audio ─▶ faster-whisper ─▶ text
speaker     ◀─ daemon (permanent playback pipeline) ◀─ WebRTC (Opus) ◀─ container: Piper, sentence by sentence ◀─ text
```

Reachy's voice goes **over the same WebRTC connection**, in the other direction. The daemon's media server makes its audio track bidirectional and plays what the client sends, through a playback pipeline created only once (with head-oscillation analysis). Compared with the former path (upload then `play_sound`), this removes about 1 s per sentence: the daemon rebuilt a GStreamer pipeline for every sound, blocking its event loop. It also makes it possible to start speaking while the following sentences are still being synthesized, and to know exactly when playback ends. If the connection is not available, the server automatically falls back to `play_sound` (the `output` field tells which path was used; `SPEECH_OUTPUT` setting).

The models (`Systran/faster-whisper-small` and `rhasspy/piper-voices`, pinned revisions) are **downloaded when the image is built** and embedded in the image (about 610 MB). At runtime, nothing is downloaded and no audio or image leaves the machine.

**Two voices**: French (`fr_FR-siwis-medium`, default) and English (`en_US-lessac-medium`). `speak` and `converse` take a `language` parameter (`"en"` for English). In `converse`, it also selects the speech recognition language. Both voices are loaded by the startup warm-up: switching language during a conversation costs nothing (a simple table lookup, no loading). A language without an installed voice falls back to the French voice (the `voice` field of the result says so).

Performance measured on this host (CPU, Docker Desktop):

| Step | Before optimization | Now |
|---|---|---|
| Voice start after the call | ~1.1 s (synthesis + `play_sound`) | **0.03 s** (WebRTC, sentence by sentence) |
| Time added beyond the sentence duration | +1.7 s | **+0.3 s** |
| End of the speaker's sentence → text | ~3.1 s | **1.0 to 1.25 s** (speculative transcription after 300 ms of silence) |
| First call (model loading, connection) | +3 to 5 s | **0** (startup warm-up) |
| Tool calls per conversation turn | 2 to 5 | **1** (`converse`) |
| Measured full turn (speak + listen to the answer) | 12.5 to 14.6 s | **7.75 s** |

### Enabling

In `.env`:

```
ENABLE_PHYSICAL_ACTIONS=true
ENABLE_PERCEPTION=true
```

then:

```bash
docker compose up --build -d
```

The daemon's port `8443` must be reachable from the container. On Docker Desktop, it is, through `host.docker.internal` (verified).

### Using the skill with Claude Code

The repository provides `.mcp.json`, which declares the `minireachy` server at `http://127.0.0.1:8765/mcp`, and the [`reachy-conversation`](.claude/skills/reachy-conversation/SKILL.md) skill. Open Claude Code in this folder, approve the project's MCP server, then ask for example "talk with Reachy" or type `/reachy-conversation`.

The skill:

1. checks safety, the daemon and the media stream;
2. greets aloud;
3. chains `listen` → (`capture_image` if useful) → `speak` turns, with antenna gestures and recorded emotions;
4. handles "stop", "goodbye" and silences;
5. brings the head back to neutral at the end of the conversation.

Claude acts as the "brain": each turn corresponds to a few MCP tool calls. Expect 4 to 10 s of latency per exchange.

### Validated on the real robot (2026-10-11)

* 1920×1080 image received from the container (about 55 frames/s), 16 kHz audio.
* Reachy said a welcome sentence, then heard and correctly transcribed a question asked aloud ("Je veux que tu me dises quelle est la couleur de mon t-shirt"). It then looked with its camera and answered aloud.
* The axis directions were verified with the camera: a negative pitch raises the head, and a negative yaw turns it to the robot's right. The `turn_toward_sound` convention (positive yaw = left) is therefore correct.
* The voice sent over WebRTC is indeed played by the daemon (its log shows "Setting up incoming audio playback").
* If the daemon's media pipeline stops ("Internal data stream error" in the Reachy Mini Control log, observed once), the server detects it (no frame within 8 s) and retries in a loop. Reachy Mini Control must then be restarted; the server reconnects on its own.

### MCP call examples

Read the state (JSON-RPC `tools/call`):

```json
{"jsonrpc":"2.0","id":3,"method":"tools/call",
 "params":{"name":"get_full_state","arguments":{"with_doa":true}}}
```

Nod, then wait for the end of the movement (physical actions enabled):

```json
{"jsonrpc":"2.0","id":4,"method":"tools/call",
 "params":{"name":"goto_pose","arguments":{"duration":1.0,"head_pose":{"pitch":0.25},"interpolation":"minjerk"}}}
```

Typical response: `{"accepted_by_daemon": true, "completion_confirmed": false, "move_uuid": "…", "preconditions": {...}}`. Then:

```json
{"jsonrpc":"2.0","id":5,"method":"tools/call",
 "params":{"name":"wait_for_move","arguments":{"move_uuid":"<uuid>","timeout_s":5}}}
```

Play an emotion:

```json
{"jsonrpc":"2.0","id":6,"method":"tools/call",
 "params":{"name":"play_recorded_move","arguments":{"dataset_name":"pollen-robotics/reachy-mini-emotions-library","move_name":"cheerful1"}}}
```

With the SDK Python client, from a container on the Compose network:

```python
from mcp import Client
async with Client("http://minireachy-mcp:8765/mcp") as c:
    print((await c.call_tool("get_motor_status", {})).structured_content)   # {'mode': 'enabled'}
```

## 10. Security restrictions

* **Privacy**: without `ENABLE_PERCEPTION=true`, no image and no listening. The WebRTC stream is not even opened (tested). Images and audio are processed in memory and are never written to disk or sent off the machine. While the stream is open (from the startup warm-up or the last perception/voice call, until `MEDIA_IDLE_DISCONNECT_S` of inactivity), the microphones are captured **continuously** into an in-memory buffer of the last 60 s, so that `listen` can estimate the noise level; that buffer is erased when the stream goes idle.
* **Refused by default**: without `ENABLE_PHYSICAL_ACTIONS=true`, no physical tool sends a request. Even the pre-check read is not performed (tested for every tool). The presence of a tool is never an authorization.
* **`goto_pose` guardrails**: by default, head translation ±0.03 m, roll/pitch ±0.6 rad, head yaw ±1.2 rad, body yaw ±1.5 rad, antennas ±3.0 rad, duration from 0.3 to 10 s. These adjustable bounds are **specific to the MCP server**. They are not robot specifications: the daemon's OpenAPI schema publishes none.
* **Preconditions**: before any movement, the server reads the daemon state. It refuses if the state is not `running` and warns if the motors are not in `enabled` mode.
* **Accepted command ≠ completed action**: movements return `completion_confirmed: false` and a `move_uuid`. Completion is checked with `wait_for_move` then `get_full_state`. The API cannot distinguish a completed, stopped or failed movement.
* **No command retries**: only GETs are retried. If a command times out after being sent, the server returns `uncertain_outcome` ("the daemon MAY have executed the command") and never sends it again.
* **No hardware emergency stop**: the daemon exposes none. `stop_all_moves` is a software stop of movements, which stops neither apps nor wobbling.
* **Filtered inputs**: `play_sound` only accepts a file name, never an absolute path. Path segments are encoded, including `/`. `install_app` only sends the exact record found in the daemon's catalog.
* **Not exposed**: `set_target` (instant jump, no duration), saving and deleting the Hugging Face token (the secret would transit through the LLM), the browser OAuth flow, `health-check` (desktop application watchdog), the HTML dashboard and the STL meshes.
* **Container**: unprivileged `mcp` user (uid 10001), read-only filesystem, `cap_drop: ALL`, `no-new-privileges`, no Docker socket mounted, no `privileged`, no host network.
* **Logs**: each tool call receives a `cid` identifier, reused in requests to the daemon (`X-Request-ID` header). Action arguments are logged for audit, except file contents. Response bodies are not logged.

## 11. Diagnostic commands

```bash
docker compose ps
```

```bash
docker compose logs --tail 100 minireachy-mcp
```

```bash
docker compose exec minireachy-mcp python -m minireachy_mcp.diagnose --drift
```

```bash
docker inspect minireachy-mcp-minireachy-mcp-1 --format "{{.State.Health.Status}} restarts={{.RestartCount}}"
```

Logs follow the format `tool_call cid=… tool=… risk=… outcome=…` and `daemon_request cid=… method=… path=… status=…`.

## 12. Tests and shutdown

Unit and integration tests, in a container **without network** (`network_mode: none`, no possible access to the robot). They use a simulated daemon, served over real HTTP:

```bash
docker compose --profile test run --rm tests
```

End-to-end tests through the running stack. They only call read tools, on the real daemon:

```bash
docker compose --profile e2e run --rm e2e
```

The tests cover configuration, the HTTP client (4xx/5xx errors, invalid JSON, oversized responses, timeouts, refused connection, path encoding), the safety gates of every guarded tool, the guardrails, uncertain outcomes, tool discovery and calls with a real MCP client (in-memory and over Streamable HTTP), Bearer authentication, DNS-rebinding protection, `/healthz`, shutdown on SIGTERM, the drift between the OpenAPI and the inventory, and the Docker/Compose configuration.

Regenerate the inventory from the repository root (under Git Bash for Windows, prefix the command with `MSYS_NO_PATHCONV=1`):

```bash
docker compose --profile test run --rm -v ./scripts:/app/scripts:ro -v ./docs:/app/docs tests python scripts/gen_inventory.py
```

Shutdown (SIGTERM, graceful stop in about 2 s, exit code 0, no robot action):

```bash
docker compose down
```

## 13. Known limitations and unverified points

* **On the real robot, only `goto_pose` and `speak` were executed**, with the operator's approval. The other 28 implemented endpoints are only tested against the simulated daemon, including all other physical, administration and configuration commands. Their real behavior remains to be validated by the operator, with the robot clear of obstacles.
* **Head that does not return exactly in place**: on this robot, the head roughly reaches downward or sideways poses, but stops short of the target on return (3 to 14° gap observed, stable over time). The antennas follow their setpoints faithfully. The cause is not identified (mechanics, calibration?) and the daemon does not expose the head's target position.
* During one test, the daemon stopped about 25 s after two movements, with no visible error on the MCP side. The incident did not reproduce.
* 29 endpoints were successfully called on the real daemon v1.8.0, through the containerized server. `read_audio_parameter` was also called, but it returns HTTP 500 "Errno 13 Access denied" on this host. This is a USB access problem on the daemon side, the same with a direct call, and the error is correctly reported.
* The motion bounds are conservative choices of the MCP server. The real mechanical limits are not documented by the API.
* `goto_pose` only accepts the x/y/z/roll/pitch/yaw representation. The API's 4×4 matrix form is not exposed as input, because it is harder to validate. It remains available for reading.
* Linux networking (`host-gateway`, `socat` relay) is documented but was not tested here. Only Docker Desktop on Windows was.
* Authentication is a static Bearer token, with no built-in OAuth or TLS.
* The exact response formats of the command endpoints (`stop_move`, `set_mode`…) are declared in the API as plain string dictionaries. They are therefore returned as is in `daemon_response`.
* The schema declares `info.version = 0.1.0` (FastAPI default value). The real daemon version (1.8.0) can be read from `get_daemon_status`.

## 14. License

This project is released under the [MIT License](LICENSE).

The Docker image bundles third-party software and models that keep their own licenses (notably faster-whisper and its model, Piper and its voices, aiortc, GStreamer). Check them before redistributing the image, in particular the license of `piper-tts` (the Piper project is GPL-licensed) and of each voice in [`rhasspy/piper-voices`](https://huggingface.co/rhasspy/piper-voices).
