"""Media, sound, volume and audio DSP tools."""

import base64
import binascii
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..client import seg
from ..errors import SafetyGateError
from ..models import SAFE_FILENAME, AudioParam
from ..safety import Risk
from ._common import Deps, accepted, register

_AUDIO_EXTENSIONS = (".wav", ".mp3", ".ogg", ".flac")


def register_tools(server: MCPServer, deps: Deps) -> None:
    c = deps.client

    # -- media hardware --------------------------------------------------------------

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/media/status",))
    async def get_media_status() -> dict[str, Any]:
        """Get the camera/audio hardware status: available, released (handed to a direct client), no_media."""
        return await c.get("/api/media/status")

    @register(server, deps, risk=Risk.ADMIN, endpoints=("POST /api/media/release",))
    async def release_media() -> dict[str, Any]:
        """Release the camera and audio hardware so another program can open them directly.

        While released, the daemon's own sound playback / DoA may be unavailable. Undo with acquire_media.
        """
        return accepted("release_media", await c.post("/api/media/release"))

    @register(server, deps, risk=Risk.ADMIN, endpoints=("POST /api/media/acquire",))
    async def acquire_media() -> dict[str, Any]:
        """Re-acquire the camera and audio hardware for the daemon (undo release_media)."""
        return accepted("acquire_media", await c.post("/api/media/acquire"))

    # -- sounds ----------------------------------------------------------------------

    @register(server, deps, risk=Risk.PHYSICAL, endpoints=("POST /api/media/play_sound",))
    async def play_sound(
        file: Annotated[
            str,
            Field(
                pattern=SAFE_FILENAME,
                description="Bare file name: a built-in daemon asset or a file uploaded with upload_sound "
                "(see list_uploaded_sounds). Absolute paths are not accepted by this server.",
            ),
        ],
    ) -> dict[str, Any]:
        """Play a sound file on the robot's speaker. Returns when playback is started, not finished.

        Stop it with stop_sound. If wobbling is enabled the head also moves with the audio.
        """
        if ".." in file:
            raise SafetyGateError("file must be a bare file name.")
        return accepted("play_sound", await c.post("/api/media/play_sound", json_body={"file": file}))

    @register(server, deps, risk=Risk.STOP, endpoints=("POST /api/media/stop_sound",))
    async def stop_sound() -> dict[str, Any]:
        """Stop what the robot is saying or playing (WebRTC voice and daemon sound file). Always allowed."""
        speaker = getattr(getattr(deps.media, "link", None), "speaker", None)
        voice_cleared = False
        if speaker is not None:
            voice_cleared = speaker.busy
            speaker.clear()
        return {"voice_stream_cleared": voice_cleared, "daemon_response": await c.post("/api/media/stop_sound")}

    @register(server, deps, risk=Risk.CONFIG, endpoints=("POST /api/media/sounds/upload",))
    async def upload_sound(
        filename: Annotated[
            str, Field(pattern=SAFE_FILENAME, description="Target name, e.g. 'hello.wav'. Overwrites a same-named file.")
        ],
        content_base64: Annotated[str, Field(description="Audio file content, base64-encoded.")],
    ) -> dict[str, Any]:
        """Upload an audio file to the daemon's temporary sound directory (does not play it).

        Size is limited by MAX_SOUND_UPLOAD_BYTES. Returns the daemon-side path. Play it with
        play_sound(filename).
        """
        if ".." in filename or not filename.lower().endswith(_AUDIO_EXTENSIONS):
            raise SafetyGateError(f"filename must be a bare name ending with one of {', '.join(_AUDIO_EXTENSIONS)}.")
        limit = deps.settings.max_sound_upload_bytes
        if len(content_base64) > (limit * 4) // 3 + 8:
            raise SafetyGateError(f"File exceeds the {limit}-byte upload limit.")
        try:
            data = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise SafetyGateError("content_base64 is not valid base64.") from None
        if not data or len(data) > limit:
            raise SafetyGateError(f"File must be between 1 and {limit} bytes.")
        response = await c.post(
            "/api/media/sounds/upload", files={"file": (filename, data, "application/octet-stream")}
        )
        return {"uploaded": True, "filename": filename, "size_bytes": len(data), "daemon_response": response}

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/media/sounds",))
    async def list_uploaded_sounds() -> dict[str, Any]:
        """List sound files previously uploaded to the daemon's temporary directory."""
        return await c.get("/api/media/sounds")

    @register(server, deps, risk=Risk.CONFIG, endpoints=("DELETE /api/media/sounds/{filename}",))
    async def delete_uploaded_sound(
        filename: Annotated[str, Field(pattern=SAFE_FILENAME, description="A name from list_uploaded_sounds.")],
    ) -> dict[str, Any]:
        """Delete an uploaded sound from the daemon's temporary directory (built-in assets are untouched)."""
        if ".." in filename:
            raise SafetyGateError("filename must be a bare file name.")
        return {"daemon_response": await c.delete(f"/api/media/sounds/{seg(filename)}")}

    # -- wobbling --------------------------------------------------------------------

    @register(server, deps, risk=Risk.PHYSICAL, endpoints=("POST /api/media/wobbling/enable",))
    async def enable_wobbling() -> dict[str, Any]:
        """Enable audio-reactive head wobbling: sounds played by the daemon make the head move subtly."""
        return accepted("enable_wobbling", await c.post("/api/media/wobbling/enable"))

    @register(server, deps, risk=Risk.PHYSICAL, endpoints=("POST /api/media/wobbling/disable",))
    async def disable_wobbling() -> dict[str, Any]:
        """Disable audio-reactive head wobbling and reset its head offsets (the head may move slightly)."""
        return accepted("disable_wobbling", await c.post("/api/media/wobbling/disable"))

    # -- volume ----------------------------------------------------------------------

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/volume/current",))
    async def get_volume() -> dict[str, Any]:
        """Get the speaker output volume (0–100) with the platform and audio device name."""
        return await c.get("/api/volume/current")

    @register(server, deps, risk=Risk.PHYSICAL, endpoints=("POST /api/volume/set",))
    async def set_volume(volume: Annotated[int, Field(ge=0, le=100, description="Output volume 0–100.")]) -> dict[str, Any]:
        """Set the speaker output volume. The daemon ALSO plays a test sound at the new level.

        Returns the volume reported by the daemon after the change and whether it matches.
        """
        response = await c.post("/api/volume/set", json_body={"volume": volume})
        return {
            "requested_volume": volume,
            "daemon_response": response,
            "confirmed": isinstance(response, dict) and response.get("volume") == volume,
        }

    @register(server, deps, risk=Risk.PHYSICAL, endpoints=("POST /api/volume/test-sound",))
    async def play_test_sound() -> dict[str, Any]:
        """Play the daemon's test sound on the speaker."""
        return accepted("play_test_sound", await c.post("/api/volume/test-sound"))

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/volume/microphone/current",))
    async def get_microphone_volume() -> dict[str, Any]:
        """Get the microphone input volume (0–100) with the platform and audio device name."""
        return await c.get("/api/volume/microphone/current")

    @register(server, deps, risk=Risk.CONFIG, endpoints=("POST /api/volume/microphone/set",))
    async def set_microphone_volume(
        volume: Annotated[int, Field(ge=0, le=100, description="Input volume 0–100.")],
    ) -> dict[str, Any]:
        """Set the microphone input volume. Returns the level reported by the daemon afterwards."""
        response = await c.post("/api/volume/microphone/set", json_body={"volume": volume})
        return {
            "requested_volume": volume,
            "daemon_response": response,
            "confirmed": isinstance(response, dict) and response.get("volume") == volume,
        }

    # -- XVF3800 audio DSP -----------------------------------------------------------

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/audio/config/parameter/{name}",))
    async def read_audio_parameter(
        name: Annotated[str, Field(pattern=r"^[A-Z0-9_]{1,64}$", description="XVF3800 parameter name.")],
    ) -> dict[str, Any]:
        """Read one parameter of the XVF3800 microphone-array DSP. Returns {name, values}.

        Unknown names return a 404 error; some hosts return a permission error from the USB device.
        """
        return await c.get(f"/api/audio/config/parameter/{seg(name)}")

    @register(server, deps, risk=Risk.ADMIN, endpoints=("POST /api/audio/config/apply",))
    async def apply_audio_config(
        config: Annotated[list[AudioParam], Field(min_length=1, max_length=64)],
        verify: Annotated[bool, Field(description="Ask the daemon to read values back and verify them.")] = True,
    ) -> dict[str, Any]:
        """Write a batch of XVF3800 DSP parameters (echo cancellation, beamforming, gains…).

        Wrong values can degrade audio capture until reset. Returns {"applied": bool}.
        """
        response = await c.post(
            "/api/audio/config/apply",
            json_body={"config": [p.model_dump() for p in config], "verify": verify},
        )
        return {"daemon_response": response, "applied": bool(isinstance(response, dict) and response.get("applied"))}
