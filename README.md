# minireachy-mcp

Serveur **MCP (Model Context Protocol)** pour piloter un robot **Reachy Mini** (« MiniReachy », Hugging Face / Pollen Robotics) au travers de son **daemon natif**.

* Le serveur MCP tourne **dans Docker Compose**. Le daemon MiniReachy reste sur la machine hôte et n'est jamais installé dans le conteneur.
* Le transport est **Streamable HTTP** sur `http://127.0.0.1:8765/mcp`, avec le SDK Python officiel `mcp==2.3.0` (branche v2).
* **65 outils** couvrent les 57 opérations pertinentes de l'API du daemon (v1.8.0). 10 opérations sont exclues, chacune avec une justification. Voir [docs/inventaire-api.md](docs/inventaire-api.md).
* **Perception et voix** : Reachy voit (caméra), écoute (micros, reconnaissance vocale locale faster-whisper) et parle (synthèse vocale locale Piper, voix française). Le tout passe par le flux WebRTC du daemon, sans accès USB depuis le conteneur.
* **Skill conversationnel** : [.claude/skills/reachy-conversation](.claude/skills/reachy-conversation/SKILL.md) transforme Reachy en agent conversationnel piloté par Claude.
* Les actions physiques, les actions d'administration et la perception (caméra et micros) sont **refusées par défaut**.

---

## 1. Architecture

```
 Client MCP (Claude Code, IDE, agent…)
        │  Streamable HTTP  http://127.0.0.1:8765/mcp   (publié sur la boucle locale uniquement)
        ▼
┌──────────────── conteneur minireachy-mcp (Docker Compose) ────────────────┐
│ server.py   MCPServer + /healthz + auth Bearer optionnelle + anti DNS-rebinding │
│ tools/      outils par domaine : daemon, state, motion, media, apps            │
│ safety.py   portes ENABLE_PHYSICAL_ACTIONS / ENABLE_ADMIN_ACTIONS + garde-fous  │
│ client.py   client httpx unique : délais, taille max, relance GET uniquement    │
└────────────────────────────────────────────────────────────────────────────┘
        │  HTTP  MINIREACHY_BASE_URL (par défaut http://host.docker.internal:8000)
        ▼
 Daemon Reachy Mini (natif, sur l'hôte) ── USB ── robot
```

| Fichier | Rôle |
|---|---|
| `src/minireachy_mcp/config.py` | Variables d'environnement, validation, valeurs par défaut sûres |
| `src/minireachy_mcp/client.py` | Client HTTP du daemon (timeouts, erreurs, taille des réponses, corrélation) |
| `src/minireachy_mcp/errors.py` | Erreurs typées, rédigées pour être exploitables par un LLM |
| `src/minireachy_mcp/models.py` | Modèles d'entrée reflétant les schémas OpenAPI |
| `src/minireachy_mcp/safety.py` | Classes de risque, portes de sécurité, garde-fous de mouvement |
| `src/minireachy_mcp/tools/` | Outils MCP par domaine |
| `src/minireachy_mcp/inventory.py` | Endpoints exclus et leur justification |
| `src/minireachy_mcp/diagnostics.py`, `diagnose.py` | Diagnostic réseau en 4 étapes, détection d'écart OpenAPI |
| `src/minireachy_mcp/server.py`, `__main__.py` | Assemblage du serveur, application HTTP, arrêt propre |
| `tests/` | Tests unitaires, intégration avec un daemon simulé, protocole MCP, e2e |
| `docs/api/openapi.json` | Instantané du schéma OpenAPI du daemon (référence des tests) |

## 2. Prérequis

* Docker Engine avec Docker Compose v2.24 ou plus récent. Validé avec Docker Desktop (Windows 11), Docker 29.8.2 et Compose v5.5.1.
* Le daemon Reachy Mini (application « Reachy Mini Control ») lancé sur l'hôte. Validé avec la version 1.8.0, sur un Reachy Mini Lite en USB.
* Aucun Python n'est nécessaire sur l'hôte : la construction, les tests et le diagnostic passent par Docker.

## 3. Configuration

```bash
cp .env.example .env
```

Toutes les variables sont documentées dans [.env.example](.env.example). Les principales :

| Variable | Défaut | Rôle |
|---|---|---|
| `MINIREACHY_BASE_URL` | `http://host.docker.internal:8000` | URL du daemon **vue depuis le conteneur** |
| `MINIREACHY_CONNECT_TIMEOUT` / `MINIREACHY_READ_TIMEOUT` | `3` / `15` s | Délais HTTP |
| `MINIREACHY_GET_RETRIES` | `1` | Relances, pour les lectures (GET) uniquement |
| `MINIREACHY_MAX_RESPONSE_BYTES` | `2000000` | Au-delà de cette taille, la réponse est rejetée |
| `MCP_PUBLISH_PORT` | `8765` | Port publié sur `127.0.0.1` de l'hôte |
| `MCP_PATH` | `/mcp` | Chemin de l'endpoint MCP |
| `MCP_AUTH_TOKEN` | vide | Jeton Bearer (24 caractères minimum). Vide = pas d'authentification |
| `MCP_ALLOWED_HOSTS` | loopback + `minireachy-mcp:*` | En-têtes `Host` acceptés (protection DNS rebinding) |
| `ENABLE_PHYSICAL_ACTIONS` | `false` | Autorise mouvements, couple moteur, son et démarrage d'apps |
| `ENABLE_ADMIN_ACTIONS` | `false` | Autorise le cycle de vie du daemon, l'installation d'apps, la libération des médias et le DSP audio |
| `ENABLE_PERCEPTION` | `false` | Autorise la caméra (`capture_image`) et l'écoute (`listen`) |
| `MEDIA_SIGNALLING_URL` | `ws://host.docker.internal:8443` | Signalisation WebRTC du serveur média du daemon |
| `STT_LANGUAGE`, `STT_BEAM_SIZE`, `TTS_LENGTH_SCALE` | `fr`, `1`, `1.0` | Langue et réglages de la reconnaissance et de la synthèse vocales |
| `LIMIT_*` | voir `.env.example` | Garde-fous de `goto_pose`, propres au serveur MCP |

Aucun secret ne figure dans l'image ni dans le dépôt : `.env` est ignoré par git et par le contexte de build Docker.

## 4. Lancement

```bash
docker compose up --build -d
```

```bash
docker compose ps
```

```bash
docker compose logs -f minireachy-mcp
```

L'état `healthy` ne concerne **que le serveur MCP**. Si le daemon est arrêté, le conteneur reste sain et ne redémarre pas en boucle. Ce comportement a été vérifié : avec un daemon injoignable, le conteneur reste `healthy` et `RestartCount=0`. Les outils, eux, renvoient alors `daemon_unreachable`.

Pour autoriser les actions physiques, l'opérateur modifie `.env` :

```
ENABLE_PHYSICAL_ACTIONS=true
```

puis relance :

```bash
docker compose up -d
```

## 5. Réseau selon le système hôte

Le daemon tourne sur l'hôte et le serveur MCP dans un conteneur. Il faut donc distinguer :

| Point de vue | Daemon | Serveur MCP |
|---|---|---|
| Hôte | `http://localhost:8000` | `http://127.0.0.1:8765/mcp` |
| Conteneur | `http://host.docker.internal:8000` | écoute sur `0.0.0.0:8765` (interne au conteneur) |
| Autre conteneur Compose | `http://host.docker.internal:8000` | `http://minireachy-mcp:8765/mcp` |

**Windows et macOS (Docker Desktop)** : `host.docker.internal` est fourni par Docker Desktop. Ce cas a été vérifié sous Windows : le nom se résout en `192.168.65.254` et le daemon répond.

**Linux** : `compose.yaml` déclare `extra_hosts: host.docker.internal:host-gateway`, qui fait pointer ce nom vers la passerelle de l'hôte. Attention, ce n'est **pas suffisant** si le daemon n'écoute que sur `127.0.0.1` : la passerelle Docker n'atteint pas la boucle locale de l'hôte. Solutions possibles, de la plus restrictive à la moins restrictive :

1. Un relais TCP sur l'hôte, limité à l'interface du pont Docker. Par exemple :
   ```bash
   socat TCP-LISTEN:8000,bind=172.17.0.1,reuseaddr,fork TCP:127.0.0.1:8000
   ```
   Il faut ensuite vérifier l'IP réelle du pont (`ip addr show docker0`) et le pare-feu.
2. Faire écouter le daemon sur l'interface du pont Docker, avec un pare-feu qui n'autorise que les sous-réseaux Docker.
3. `MINIREACHY_BASE_URL` pointant vers une autre IP de l'hôte, filtrée par le pare-feu.

Le mode `network_mode: host` n'est volontairement pas utilisé. **Le cas Linux n'a pas été testé dans cet environnement.**

**Observation de sécurité sur l'hôte de développement** : le daemon v1.8.0 écoute sur `0.0.0.0:8000`, donc sur toutes les interfaces, sans authentification. Il est joignable depuis le réseau local, indépendamment de ce projet. Je n'ai pas modifié sa configuration. Il est conseillé de restreindre le port 8000 par le pare-feu de l'hôte (par exemple, sous Windows, autoriser seulement `127.0.0.1` et le sous-réseau Docker Desktop).

## 6. Vérifier la connectivité vers MiniReachy

Depuis le conteneur, quatre étapes distinctes sont vérifiées : résolution DNS, port TCP, réponse HTTP, puis état fonctionnel du daemon.

```bash
docker compose exec minireachy-mcp python -m minireachy_mcp.diagnose
```

Pour ajouter la comparaison entre l'OpenAPI en ligne et l'inventaire des outils :

```bash
docker compose exec minireachy-mcp python -m minireachy_mcp.diagnose --drift
```

Santé du serveur MCP seul :

```bash
curl http://127.0.0.1:8765/healthz
```

Joignabilité du daemon (répond 503 si le daemon est injoignable ou n'est pas `running`) :

```bash
curl http://127.0.0.1:8765/healthz/daemon
```

Le même diagnostic existe sous forme d'outil MCP : `diagnose_connectivity`. Une étape `daemon_status` OK signifie seulement que le daemon déclare l'état `running`. Cela ne prouve pas que le matériel du robot est en bon état.

## 7. Adresse et transport MCP

* Transport : **Streamable HTTP** (SDK `mcp` 2.3.0, sessions avec état, réponses SSE).
* URL : `http://127.0.0.1:8765/mcp`, publiée **uniquement sur la boucle locale** de l'hôte.
* Protection DNS rebinding active : seuls les en-têtes `Host` listés dans `MCP_ALLOWED_HOSTS` sont acceptés.
* Authentification Bearer optionnelle avec `MCP_AUTH_TOKEN`. Les routes `/healthz*` n'en ont pas besoin.

**Accès depuis une autre machine** (non configuré par défaut) :

1. Définir `MCP_AUTH_TOKEN`.
2. Placer un reverse proxy TLS (Caddy, Traefik, nginx) devant le service.
3. Ajouter le nom d'hôte public à `MCP_ALLOWED_HOSTS`.
4. Restreindre les adresses sources au pare-feu.

Ne publiez jamais `0.0.0.0:8765` sans ces protections. Le jeton est statique : il n'y a ni OAuth ni rotation automatique.

## 8. Configurer un client MCP

**Claude Code** :

```bash
claude mcp add --transport http minireachy http://127.0.0.1:8765/mcp
```

Avec un jeton :

```bash
claude mcp add --transport http minireachy http://127.0.0.1:8765/mcp --header "Authorization: Bearer VOTRE_JETON"
```

**Client générique** (fichier `.mcp.json`, Cursor, VS Code…) :

```json
{
  "mcpServers": {
    "minireachy": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer VOTRE_JETON" }
    }
  }
}
```

Supprimez `headers` si `MCP_AUTH_TOKEN` est vide. Les commandes de configuration de ces clients n'ont pas été exécutées dans cet environnement. La découverte et l'appel des outils ont en revanche été validés avec le client MCP officiel du SDK, en HTTP, depuis un conteneur (`tests/test_e2e.py`).

## 9. Outils disponibles

Tous les outils renvoient du JSON structuré (`structuredContent`). Les erreurs sont renvoyées avec `isError=true` et un code explicite : `refused_by_safety_policy`, `daemon_unreachable`, `uncertain_outcome`, `daemon_validation_error`, `daemon_http_error`, `invalid_response`, `response_too_large`. Le détail endpoint par endpoint se trouve dans [docs/inventaire-api.md](docs/inventaire-api.md).

**Lecture et diagnostic (toujours disponibles, sans effet de bord)** :

| Domaine | Outils |
|---|---|
| Daemon | `get_daemon_status`, `get_hardware_id`, `get_robot_app_lock_status`, `diagnose_connectivity`, `get_server_safety_config` |
| État | `get_full_state`, `get_head_pose`, `get_body_yaw`, `get_antenna_positions`, `get_direction_of_arrival`, `get_motor_status` |
| Mouvements | `list_running_moves`, `get_move_status`, `wait_for_move`, `list_recorded_moves` |
| Caméra, cinématique | `get_camera_specs`, `get_kinematics_info`, `get_urdf` (tronqué par défaut) |
| Médias, audio | `get_media_status`, `list_uploaded_sounds`, `get_volume`, `get_microphone_volume`, `read_audio_parameter` |
| Apps | `list_apps` (forme compacte, filtre), `get_current_app_status`, `check_app_updates`, `get_app_job_status` |
| Hugging Face | `get_hf_auth_status`, `get_relay_status`, `get_central_robot_status` |

**Arrêts (toujours autorisés)** : `stop_move`, `stop_all_moves`, `stop_sound`, `stop_current_app`.

**Configuration non physique (autorisée)** : `set_microphone_volume`, `upload_sound` (base64, taille limitée, extensions audio uniquement), `delete_uploaded_sound`, `refresh_relay`.

**Physiques (`ENABLE_PHYSICAL_ACTIONS=true`)** :

| Outil | Effet |
|---|---|
| `goto_pose` | Mouvement de la tête, des antennes et/ou du corps, interpolé sur `duration`. Soumis aux garde-fous |
| `wake_up` / `goto_sleep` | Mouvements intégrés de réveil et de mise en veille |
| `play_recorded_move` | Joue un mouvement enregistré (émotions, danses…) |
| `set_motor_mode` | `enabled` / `disabled` (plus de couple) / `gravity_compensation`, avec relecture pour confirmer |
| `play_sound`, `play_test_sound`, `set_volume` | Son sur le haut-parleur. `set_volume` joue aussi un son de test |
| `enable_wobbling` / `disable_wobbling` | Oscillations de la tête réactives à l'audio |
| `start_app`, `restart_current_app` | Les apps pilotent le robot en dehors des garde-fous de ce serveur |

**Administration (`ENABLE_ADMIN_ACTIONS=true`)** : `install_app`, `install_private_space`, `remove_app`, `update_app`, `release_media`, `acquire_media`, `apply_audio_config`.

**Physique + administration (les deux interrupteurs sont nécessaires)** : `start_daemon`, `stop_daemon`, `restart_daemon`.

**Perception et voix** :

| Outil | Interrupteur | Effet |
|---|---|---|
| `get_perception_status` | aucun (lecture) | État du flux WebRTC (images et audio reçus) et des modèles vocaux. N'ouvre pas le flux |
| `capture_image` | `ENABLE_PERCEPTION` | Photo JPEG de la caméra de la tête (ce que voit Reachy), redimensionnable |
| `listen` | `ENABLE_PERCEPTION` | Attend qu'on parle, enregistre jusqu'à une pause et transcrit (faster-whisper local). Ignore les bruits et la fin de sa propre voix, et renvoie la direction de la voix |
| `converse` | `ENABLE_PERCEPTION` (+ `ENABLE_PHYSICAL_ACTIONS` pour parler et faire un geste) | **Un tour de conversation en un seul appel** : dit une phrase (avec un geste d'antennes en parallèle), écoute la réponse, la transcrit et peut prendre une photo. Renvoie les temps mesurés |
| `speak` | `ENABLE_PHYSICAL_ACTIONS` | Synthèse vocale locale (Piper), envoyée en continu sur la connexion WebRTC (repli : upload puis `play_sound`). Rend la main à la fin de la lecture |
| `turn_toward_sound` | `ENABLE_PHYSICAL_ACTIONS` | Tourne la tête (lacet) vers la direction de la dernière voix détectée |

## Perception, voix et agent conversationnel

### Fonctionnement

Le daemon exécute un serveur média GStreamer (`webrtcsink`) qui diffuse la caméra et les micros en WebRTC. Son serveur de signalisation tourne sur le port `8443` de l'hôte. Ce flux n'est **pas** dans l'OpenAPI : je l'ai identifié dans le code du daemon (`reachy_mini/media/media_server.py`). C'est le canal officiel utilisé par le SDK distant et les navigateurs.

Le serveur MCP s'y connecte comme un client ordinaire (bibliothèque aiortc), à la demande : au premier `capture_image` ou `listen`, en environ 3 s. Il se reconnecte automatiquement si le daemon redémarre. Le conteneur n'accède donc jamais directement à l'USB.

```
micros/caméra ─ daemon (hôte) ─ WebRTC :8443 ─▶ conteneur : aiortc ─▶ image JPEG / audio 16 kHz ─▶ faster-whisper ─▶ texte
haut-parleur  ◀─ daemon (pipeline de lecture permanent) ◀─ WebRTC (Opus) ◀─ conteneur : Piper, phrase par phrase ◀─ texte
```

La voix de Reachy passe **par la même connexion WebRTC**, dans l'autre sens. Le serveur média du daemon rend sa piste audio bidirectionnelle et joue ce que le client envoie, via un pipeline de lecture créé une seule fois (avec l'analyse des oscillations de tête). Par rapport à l'ancien chemin (upload puis `play_sound`), cela supprime environ 1 s par phrase : le daemon y reconstruisait un pipeline GStreamer à chaque son, en bloquant sa boucle d'événements. Cela permet aussi de commencer à parler pendant que les phrases suivantes sont encore synthétisées, et de savoir précisément quand la lecture se termine. Si la connexion n'est pas disponible, le serveur revient automatiquement à `play_sound` (le champ `output` indique le chemin utilisé ; réglage `SPEECH_OUTPUT`).

Les modèles (`Systran/faster-whisper-small` et `rhasspy/piper-voices`, révisions épinglées) sont **téléchargés à la construction de l'image** et intégrés à l'image (environ 550 Mo). À l'exécution, rien n'est téléchargé et aucun audio ni aucune image ne sort de la machine.

Performances mesurées sur cet hôte (CPU, Docker Desktop) :

| Étape | Avant optimisation | Maintenant |
|---|---|---|
| Début de la voix après l'appel | ~1,1 s (synthèse + `play_sound`) | **0,03 s** (WebRTC, phrase par phrase) |
| Temps ajouté au-delà de la durée de la phrase | +1,7 s | **+0,3 s** |
| Fin de la phrase de l'interlocuteur → texte | ~3,1 s | **1,0 à 1,25 s** (transcription spéculative dès 300 ms de silence) |
| Premier appel (chargement des modèles, connexion) | +3 à 5 s | **0** (préchauffage au démarrage) |
| Appels d'outils par tour de conversation | 2 à 5 | **1** (`converse`) |
| Tour complet mesuré (parler + écouter la réponse) | 12,5 à 14,6 s | **7,75 s** |

### Activer

Dans `.env` :

```
ENABLE_PHYSICAL_ACTIONS=true
ENABLE_PERCEPTION=true
```

puis :

```bash
docker compose up --build -d
```

Le port `8443` du daemon doit être joignable depuis le conteneur. Sur Docker Desktop, c'est le cas via `host.docker.internal` (vérifié).

### Utiliser le skill avec Claude Code

Le dépôt fournit `.mcp.json`, qui déclare le serveur `minireachy` sur `http://127.0.0.1:8765/mcp`, et le skill [`reachy-conversation`](.claude/skills/reachy-conversation/SKILL.md). Ouvrez Claude Code dans ce dossier, approuvez le serveur MCP du projet, puis demandez par exemple « parle avec Reachy » ou tapez `/reachy-conversation`.

Le skill :

1. vérifie la sécurité, le daemon et le flux média ;
2. salue à voix haute ;
3. enchaîne les tours `listen` → (`capture_image` si utile) → `speak`, avec des gestes d'antennes et des émotions enregistrées ;
4. gère « stop », « au revoir » et les silences ;
5. ramène la tête au neutre en fin de conversation.

C'est Claude qui sert de « cerveau » : chaque tour correspond à quelques appels d'outils MCP. Il faut compter 4 à 10 s de latence par échange.

### Validé sur le robot réel (11/10/2026)

* Image 1920×1080 reçue depuis le conteneur (environ 55 images/s), audio 16 kHz.
* Reachy a dit une phrase de bienvenue, puis a entendu et transcrit correctement une question posée oralement (« Je veux que tu me dises quelle est la couleur de mon t-shirt »). Il a ensuite regardé avec sa caméra et répondu à voix haute.
* Le sens des axes a été vérifié par la caméra : un tangage négatif lève la tête, et un lacet négatif la tourne vers la droite du robot. La convention de `turn_toward_sound` (lacet positif = gauche) est donc correcte.
* La voix envoyée par WebRTC est bien jouée par le daemon (son journal indique « Setting up incoming audio playback »).
* Si le pipeline média du daemon s'arrête (« Internal data stream error » dans le journal de Reachy Mini Control, observé une fois), le serveur le détecte (aucune image en 8 s) et réessaie en boucle. Il faut alors relancer Reachy Mini Control ; le serveur se reconnecte seul.

### Exemples d'appels MCP

Lire l'état (JSON-RPC `tools/call`) :

```json
{"jsonrpc":"2.0","id":3,"method":"tools/call",
 "params":{"name":"get_full_state","arguments":{"with_doa":true}}}
```

Hocher la tête, puis attendre la fin du mouvement (actions physiques activées) :

```json
{"jsonrpc":"2.0","id":4,"method":"tools/call",
 "params":{"name":"goto_pose","arguments":{"duration":1.0,"head_pose":{"pitch":0.25},"interpolation":"minjerk"}}}
```

Réponse type : `{"accepted_by_daemon": true, "completion_confirmed": false, "move_uuid": "…", "preconditions": {...}}`. Ensuite :

```json
{"jsonrpc":"2.0","id":5,"method":"tools/call",
 "params":{"name":"wait_for_move","arguments":{"move_uuid":"<uuid>","timeout_s":5}}}
```

Jouer une émotion :

```json
{"jsonrpc":"2.0","id":6,"method":"tools/call",
 "params":{"name":"play_recorded_move","arguments":{"dataset_name":"pollen-robotics/reachy-mini-emotions-library","move_name":"cheerful1"}}}
```

Avec le client Python du SDK, depuis un conteneur du réseau Compose :

```python
from mcp import Client
async with Client("http://minireachy-mcp:8765/mcp") as c:
    print((await c.call_tool("get_motor_status", {})).structured_content)   # {'mode': 'enabled'}
```

## 10. Restrictions de sécurité

* **Vie privée** : sans `ENABLE_PERCEPTION=true`, ni image ni écoute. Le flux WebRTC n'est même pas ouvert (testé). Les images et l'audio sont traités en mémoire et ne sont jamais écrits sur disque ni envoyés hors de la machine.
* **Refus par défaut** : sans `ENABLE_PHYSICAL_ACTIONS=true`, aucun outil physique n'envoie de requête. Même la lecture de pré-vérification n'est pas faite (testé pour chaque outil). La présence d'un outil ne vaut jamais autorisation.
* **Garde-fous de `goto_pose`** : par défaut, translation de la tête ±0,03 m, roulis/tangage ±0,6 rad, lacet de la tête ±1,2 rad, lacet du corps ±1,5 rad, antennes ±3,0 rad, durée de 0,3 à 10 s. Ces bornes, réglables, sont **propres au serveur MCP**. Ce ne sont pas des spécifications du robot : le schéma OpenAPI du daemon n'en publie aucune.
* **Pré-conditions** : avant tout mouvement, le serveur lit l'état du daemon. Il refuse si l'état n'est pas `running` et avertit si les moteurs ne sont pas en mode `enabled`.
* **Commande acceptée ≠ action terminée** : les mouvements renvoient `completion_confirmed: false` et un `move_uuid`. La fin se vérifie avec `wait_for_move` puis `get_full_state`. L'API ne permet pas de distinguer un mouvement terminé, arrêté ou en échec.
* **Pas de relance des commandes** : seuls les GET sont relancés. Si une commande expire après son envoi, le serveur renvoie `uncertain_outcome` (« le daemon a PEUT-ÊTRE exécuté la commande ») et ne la renvoie jamais.
* **Pas d'arrêt d'urgence matériel** : le daemon n'en expose aucun. `stop_all_moves` est un arrêt logiciel des mouvements, qui n'arrête ni les apps ni le wobbling.
* **Entrées filtrées** : `play_sound` n'accepte qu'un nom de fichier, jamais de chemin absolu. Les segments de chemin sont encodés, y compris `/`. `install_app` n'envoie que l'enregistrement exact trouvé dans le catalogue du daemon.
* **Non exposés** : `set_target` (saut instantané, sans durée), l'enregistrement et la suppression du jeton Hugging Face (le secret transiterait par le LLM), le flux OAuth navigateur, `health-check` (watchdog de l'application de bureau), le tableau de bord HTML et les maillages STL.
* **Conteneur** : utilisateur non privilégié `mcp` (uid 10001), système de fichiers en lecture seule, `cap_drop: ALL`, `no-new-privileges`, aucun socket Docker monté, pas de `privileged`, pas de réseau hôte.
* **Journaux** : chaque appel d'outil reçoit un identifiant `cid`, repris dans les requêtes vers le daemon (en-tête `X-Request-ID`). Les arguments des actions sont journalisés pour l'audit, sauf le contenu des fichiers. Les corps de réponse ne sont pas journalisés.

## 11. Commandes de diagnostic

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

Les journaux suivent le format `tool_call cid=… tool=… risk=… outcome=…` et `daemon_request cid=… method=… path=… status=…`.

## 12. Tests et arrêt

Tests unitaires et d'intégration, dans un conteneur **sans réseau** (`network_mode: none`, aucun accès possible au robot). Ils utilisent un daemon simulé, servi en HTTP réel :

```bash
docker compose --profile test run --rm tests
```

Tests de bout en bout via la pile en cours d'exécution. Ils n'appellent que des outils de lecture, sur le daemon réel :

```bash
docker compose --profile e2e run --rm e2e
```

Les tests couvrent la configuration, le client HTTP (erreurs 4xx/5xx, JSON invalide, réponses trop grandes, timeouts, connexion refusée, encodage des chemins), les portes de sécurité de chaque outil gardé, les garde-fous, les résultats incertains, la découverte et l'appel des outils avec un vrai client MCP (en mémoire et en Streamable HTTP), l'authentification Bearer, la protection DNS rebinding, `/healthz`, l'arrêt sur SIGTERM, l'écart entre l'OpenAPI et l'inventaire, et la configuration Docker/Compose.

Régénérer l'inventaire depuis la racine du dépôt (sous Git Bash pour Windows, préfixer la commande de `MSYS_NO_PATHCONV=1`) :

```bash
docker compose --profile test run --rm -v ./scripts:/app/scripts:ro -v ./docs:/app/docs tests python scripts/gen_inventory.py
```

Arrêt (SIGTERM, arrêt propre en 2 s environ, code de sortie 0, aucune action robot) :

```bash
docker compose down
```

## 13. Limites connues et points non vérifiés

* **Sur le robot réel, seuls `goto_pose` et `speak` ont été exécutés**, avec l'accord de l'opérateur. Les 28 autres endpoints implémentés ne sont testés que contre le daemon simulé, dont toutes les autres commandes physiques, d'administration et de configuration. Leur comportement réel reste à valider par l'opérateur, robot dégagé.
* **Tête qui ne revient pas exactement en place** : sur ce robot, la tête atteint à peu près les poses vers le bas ou vers le côté, mais s'arrête avant la cible au retour (écart de 3 à 14° observé, stable dans le temps). Les antennes suivent fidèlement leurs consignes. La cause n'est pas identifiée (mécanique, étalonnage ?) et le daemon n'expose pas la position cible de la tête.
* Lors d'un essai, le daemon s'est arrêté environ 25 s après deux mouvements, sans erreur visible côté MCP. L'incident ne s'est pas reproduit.
* 29 endpoints ont été appelés avec succès sur le daemon réel v1.8.0, via le serveur conteneurisé. `read_audio_parameter` a aussi été appelé, mais il renvoie HTTP 500 « Errno 13 Access denied » sur cet hôte. C'est un problème d'accès USB côté daemon, le même en appel direct, et l'erreur est correctement remontée.
* Les bornes de mouvement sont des choix conservateurs du serveur MCP. Les limites mécaniques réelles ne sont pas documentées par l'API.
* `goto_pose` n'accepte que la représentation x/y/z/roulis/tangage/lacet. La forme matrice 4×4 de l'API n'est pas exposée en entrée, car elle est plus difficile à valider. Elle reste disponible en lecture.
* Le réseau Linux (`host-gateway`, relais `socat`) est documenté mais n'a pas été testé ici. Seul Docker Desktop sous Windows l'a été.
* L'authentification est un jeton Bearer statique, sans OAuth ni TLS intégrés.
* Les formats exacts des réponses des endpoints de commande (`stop_move`, `set_mode`…) sont déclarés dans l'API comme de simples dictionnaires de chaînes. Ils sont donc renvoyés tels quels dans `daemon_response`.
* Le schéma déclare `info.version = 0.1.0` (valeur FastAPI par défaut). La version réelle du daemon (1.8.0) se lit dans `get_daemon_status`.
