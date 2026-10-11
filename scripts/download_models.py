"""Download the local speech models at image build time (pinned revisions).

Used by the `models` stage of the Dockerfile; the runtime never downloads anything.
"""

import os
import shutil
import sys

from huggingface_hub import hf_hub_download, snapshot_download


def main() -> None:
    # piper_voices: comma-separated voice paths in rhasspy/piper-voices (e.g. French and English).
    whisper_repo, whisper_rev, piper_voices, piper_rev, dest = sys.argv[1:6]
    snapshot_download(whisper_repo, revision=whisper_rev, local_dir=f"{dest}/whisper")
    shutil.rmtree(f"{dest}/whisper/.cache", ignore_errors=True)
    os.makedirs(f"{dest}/piper", exist_ok=True)
    for piper_voice in filter(None, (v.strip() for v in piper_voices.split(","))):
        for ext in (".onnx", ".onnx.json"):
            path = hf_hub_download("rhasspy/piper-voices", piper_voice + ext, revision=piper_rev,
                                   local_dir="/tmp/piper")
            shutil.copy(path, f"{dest}/piper/{os.path.basename(path)}")
    for root, _, files in os.walk(dest):
        for f in files:
            print(os.path.join(root, f), os.path.getsize(os.path.join(root, f)))


if __name__ == "__main__":
    main()
