"""Generate docs/inventaire-api.md from the OpenAPI snapshot and the tool registry.

    docker compose --profile test run --rm -v ./scripts:/app/scripts:ro -v ./docs:/app/docs tests \
        python scripts/gen_inventory.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from minireachy_mcp.client import MiniReachyClient  # noqa: E402
from minireachy_mcp.config import Settings  # noqa: E402
from minireachy_mcp.inventory import EXCLUDED  # noqa: E402
from minireachy_mcp.safety import Risk  # noqa: E402
from minireachy_mcp.server import build_server  # noqa: E402

# Endpoints exercised against the REAL daemon (v1.8.0) through the containerised MCP server on
# 2026-10-11: read-only tools (tests/test_e2e.py, scripts/smoke_readonly.py) and, with the
# operator's explicit approval, goto_pose (scripts/nod_test.py, hello_test.py) and speak
# (scripts/conversation_smoke.py: upload + play_sound + delete).
REAL_OK = {
    "GET /api/daemon/status", "GET /api/daemon/hardware-id", "GET /api/daemon/robot-app-lock-status",
    "GET /api/motors/status", "GET /api/move/running", "GET /api/state/full", "GET /api/state/present_head_pose",
    "GET /api/state/present_body_yaw", "GET /api/state/present_antenna_joint_positions", "GET /api/state/doa",
    "GET /api/media/status", "GET /api/media/sounds", "GET /api/volume/current", "GET /api/volume/microphone/current",
    "GET /api/camera/specs", "GET /api/kinematics/info", "GET /api/kinematics/urdf", "GET /api/apps/current-app-status",
    "GET /api/apps/check-updates", "GET /api/apps/list-available", "GET /api/hf-auth/status",
    "GET /api/hf-auth/relay-status", "GET /api/hf-auth/oauth/configured", "GET /api/hf-auth/central-robot-status",
    "GET /api/move/recorded-move-datasets/list/{dataset_name}",
    "POST /api/move/goto", "POST /api/media/sounds/upload", "POST /api/media/play_sound",
    "DELETE /api/media/sounds/{filename}",
}
REAL_NOTE = {
    "GET /api/audio/config/parameter/{name}": "Appelé sur le daemon réel : HTTP 500 « Errno 13 Access denied » "
    "(accès USB refusé côté hôte, identique en appel direct). Erreur correctement remontée.",
    "GET /api/apps/list-available/{source_kind}": "Appel direct réel OK (installed) ; via MCP testé en simulation.",
    "GET /api/apps/job-status/{job_id}": "Appel direct réel : 404 pour un id inconnu ; nominal testé en simulation.",
    "POST /api/move/goto": "Robot réel : mouvements exécutés. La tête n'atteint pas exactement les poses de retour "
    "(écart de 3 à 14°, voir README) ; les antennes suivent fidèlement.",
}
RISK_FR = {
    Risk.READ: "Lecture", Risk.CONFIG: "Configuration", Risk.STOP: "Arrêt (toujours autorisé)",
    Risk.PHYSICAL: "Physique", Risk.ADMIN: "Administration", Risk.PHYSICAL_ADMIN: "Physique + administration",
    Risk.PERCEPTION: "Perception",
}
EFFECTS = {
    Risk.READ: "Aucun",
    Risk.CONFIG: "Modifie un réglage ou un fichier non physique",
    Risk.STOP: "Interrompt une activité",
    Risk.PHYSICAL: "Mouvement, couple moteur, son ou app pilotant le robot",
    Risk.ADMIN: "Cycle de vie / installation / matériel",
    Risk.PHYSICAL_ADMIN: "Cycle de vie du backend, peut faire bouger le robot",
    Risk.PERCEPTION: "Caméra ou micros",
}


def _ref(schema: dict) -> str:
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[1]
    if "anyOf" in schema:
        return " \\| ".join(_ref(s) for s in schema["anyOf"])
    if schema.get("type") == "array":
        return f"array[{_ref(schema.get('items', {}))}]" if "items" in schema else "array"
    return schema.get("type", "any") if schema else "any"


def main() -> None:
    spec = json.loads((ROOT / "docs/api/openapi.json").read_text("utf-8"))
    settings = Settings()
    _, deps = build_server(settings, MiniReachyClient(settings))
    by_endpoint: dict[str, list] = {}
    for tool in deps.registry.values():
        for ep in tool.endpoints:
            by_endpoint.setdefault(ep, []).append(tool)

    rows = []
    counts = {"real": 0, "sim": 0, "excl": 0, "blocked": 0}
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            key = f"{method.upper()} {path}"
            params = ", ".join(
                f"{p['name']} ({p['in']}{', requis' if p.get('required') else ''}: {_ref(p.get('schema', {}))})"
                for p in op.get("parameters", [])
            ) or "—"
            body = "—"
            if "requestBody" in op:
                content = op["requestBody"]["content"]
                ctype = next(iter(content))
                body = f"{_ref(content[ctype]['schema'])} ({ctype.split('/')[-1]})"
            resp = _ref(op.get("responses", {}).get("200", {}).get("content", {}).get("application/json", {}).get("schema", {}))
            tools = by_endpoint.get(key, [])
            if tools:
                # A daemon write is never "perception": converse speaks/moves through it as a physical action.
                risks = [Risk.PHYSICAL if t.risk == Risk.PERCEPTION else t.risk for t in tools]
                risk = Risk.READ if method == "get" else max(risks, key=list(Risk).index)
                tool_names = ", ".join(f"`{t.name}`" for t in tools)
                if key in REAL_OK:
                    status = "Implémenté et testé (simulation + daemon réel)"
                    counts["real"] += 1
                else:
                    status = "Implémenté, testé en simulation, non testé contre le daemon réel"
                    counts["sim"] += 1
                if key in REAL_NOTE:
                    status += ". " + REAL_NOTE[key]
                rows.append((method.upper(), path, op.get("summary", ""), params, body, resp,
                             EFFECTS[risk], RISK_FR[risk], tool_names, status))
            else:
                counts["excl"] += 1
                rows.append((method.upper(), path, op.get("summary", ""), params, body, resp, "—", "—", "—",
                             "Exclu : " + EXCLUDED[key]))

    out = [
        "# Inventaire de l'API du daemon MiniReachy",
        "",
        "Fichier généré par `scripts/gen_inventory.py` à partir de `docs/api/openapi.json` "
        f"(daemon Reachy Mini v1.8.0, {sum(len(v) for v in spec['paths'].values())} opérations) "
        "et du registre des outils MCP. Ne pas éditer à la main.",
        "",
        "Authentification : le schéma ne déclare aucun mécanisme de sécurité, et aucun n'a été "
        "nécessaire lors des appels réels.",
        "",
        f"* Implémentés et testés contre le daemon réel : **{counts['real']}**",
        f"* Implémentés, testés uniquement en simulation : **{counts['sim']}**",
        f"* Exclus avec justification : **{counts['excl']}**",
        f"* Bloqués : **{counts['blocked']}**",
        "",
        "Commandes physiques testées sur le robot réel, avec l'accord de l'opérateur : `goto_pose` et `speak` "
        "(upload + lecture d'un son). Toutes les autres commandes n'ont été testées qu'en simulation.",
        "",
        "| Méthode | Chemin | Description | Paramètres | Corps | Réponse 200 | Effets de bord | Risque | Outil(s) MCP | Statut |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|") if i != 3 else str(c) for i, c in enumerate(r)) + " |")
    tools_only = [t for t in deps.registry.values() if not t.endpoints]
    out += ["", "## Outils sans endpoint direct", ""]
    local = {
        "diagnose_connectivity": "diagnostic réseau en 4 étapes (DNS, TCP, HTTP, état du daemon).",
        "get_server_safety_config": "configuration de sécurité du serveur MCP.",
        "get_perception_status": "état du flux WebRTC et des modèles vocaux locaux.",
        "capture_image": "image de la caméra, reçue par le flux WebRTC du daemon (port 8443, non décrit dans "
        "l'OpenAPI). Testé sur le robot réel.",
    }
    out += [f"* `{t.name}` : {local.get(t.name, 'calculé localement par le serveur MCP.')}" for t in tools_only]
    out += [
        "",
        "## Flux média hors OpenAPI",
        "",
        "Le daemon exécute aussi un serveur média GStreamer `webrtcsink` avec son serveur de signalisation "
        "(`ws://<hôte>:8443`, producteur `reachymini`). Il diffuse la vidéo de la caméra et l'audio des micros. "
        "Ce flux n'apparaît pas dans l'OpenAPI ; il a été identifié dans le code du daemon "
        "(`reachy_mini/media/media_server.py`) et validé en réel depuis le conteneur. Il alimente `capture_image` "
        "et `listen`. La reconnaissance vocale (faster-whisper) et la synthèse vocale (Piper) tournent localement "
        "dans le conteneur.",
    ]
    (ROOT / "docs/inventaire-api.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(counts, len(deps.registry), "tools")


if __name__ == "__main__":
    main()
