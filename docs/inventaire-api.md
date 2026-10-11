# Inventaire de l'API du daemon MiniReachy

Fichier généré par `scripts/gen_inventory.py` à partir de `docs/api/openapi.json` (daemon Reachy Mini v1.8.0, 67 opérations) et du registre des outils MCP. Ne pas éditer à la main.

Authentification : le schéma ne déclare aucun mécanisme de sécurité, et aucun n'a été nécessaire lors des appels réels.

* Implémentés et testés contre le daemon réel : **29**
* Implémentés, testés uniquement en simulation : **28**
* Exclus avec justification : **10**
* Bloqués : **0**

Commandes physiques testées sur le robot réel, avec l'accord de l'opérateur : `goto_pose` et `speak` (upload + lecture d'un son). Toutes les autres commandes n'ont été testées qu'en simulation.

| Méthode | Chemin | Description | Paramètres | Corps | Réponse 200 | Effets de bord | Risque | Outil(s) MCP | Statut |
|---|---|---|---|---|---|---|---|---|---|
| GET | /api/apps/list-available/{source_kind} | List Available Apps | source_kind (path, requis: SourceKind) | — | array[AppInfo] | Aucun | Lecture | `list_apps`, `install_app` | Implémenté, testé en simulation, non testé contre le daemon réel. Appel direct réel OK (installed) ; via MCP testé en simulation. |
| GET | /api/apps/list-available | List All Available Apps | — | — | array[AppInfo] | Aucun | Lecture | `list_apps` | Implémenté et testé (simulation + daemon réel) |
| POST | /api/apps/install | Install App | — | AppInfo (json) | object | Cycle de vie / installation / matériel | Administration | `install_app` | Implémenté, testé en simulation, non testé contre le daemon réel |
| POST | /api/apps/remove/{app_name} | Remove App | app_name (path, requis: string) | — | object | Cycle de vie / installation / matériel | Administration | `remove_app` | Implémenté, testé en simulation, non testé contre le daemon réel |
| GET | /api/apps/job-status/{job_id} | Job Status | job_id (path, requis: string) | — | JobInfo | Aucun | Lecture | `get_app_job_status` | Implémenté, testé en simulation, non testé contre le daemon réel. Appel direct réel : 404 pour un id inconnu ; nominal testé en simulation. |
| POST | /api/apps/start-app/{app_name} | Start App | app_name (path, requis: string) | — | AppStatus | Mouvement, couple moteur, son ou app pilotant le robot | Physique | `start_app` | Implémenté, testé en simulation, non testé contre le daemon réel |
| POST | /api/apps/restart-current-app | Restart App | — | — | AppStatus | Mouvement, couple moteur, son ou app pilotant le robot | Physique | `restart_current_app` | Implémenté, testé en simulation, non testé contre le daemon réel |
| POST | /api/apps/stop-current-app | Stop App | — | — | any | Interrompt une activité | Arrêt (toujours autorisé) | `stop_current_app` | Implémenté, testé en simulation, non testé contre le daemon réel |
| GET | /api/apps/current-app-status | Current App Status | — | — | AppStatus \\| null | Aucun | Lecture | `get_current_app_status` | Implémenté et testé (simulation + daemon réel) |
| POST | /api/apps/install-private-space | Install Private Space | — | PrivateSpaceInstallRequest (json) | object | Cycle de vie / installation / matériel | Administration | `install_private_space` | Implémenté, testé en simulation, non testé contre le daemon réel |
| GET | /api/apps/check-updates | Check App Updates | force (query: boolean) | — | AppUpdatesResponse | Aucun | Lecture | `check_app_updates` | Implémenté et testé (simulation + daemon réel) |
| POST | /api/apps/update/{app_name} | Update App | app_name (path, requis: string) | — | object | Cycle de vie / installation / matériel | Administration | `update_app` | Implémenté, testé en simulation, non testé contre le daemon réel |
| POST | /api/audio/config/apply | Apply Audio Config | — | ApplyAudioConfigRequest (json) | ApplyAudioConfigResponse | Cycle de vie / installation / matériel | Administration | `apply_audio_config` | Implémenté, testé en simulation, non testé contre le daemon réel |
| GET | /api/audio/config/parameter/{name} | Read Audio Parameter | name (path, requis: string) | — | ReadAudioParameterResponse | Aucun | Lecture | `read_audio_parameter` | Implémenté, testé en simulation, non testé contre le daemon réel. Appelé sur le daemon réel : HTTP 500 « Errno 13 Access denied » (accès USB refusé côté hôte, identique en appel direct). Erreur correctement remontée. |
| GET | /api/camera/specs | Get Camera Specs | — | — | CameraSpecsResponse | Aucun | Lecture | `get_camera_specs` | Implémenté et testé (simulation + daemon réel) |
| POST | /api/daemon/start | Start Daemon | wake_up (query, requis: boolean) | — | object | Cycle de vie du backend, peut faire bouger le robot | Physique + administration | `start_daemon` | Implémenté, testé en simulation, non testé contre le daemon réel |
| POST | /api/daemon/stop | Stop Daemon | goto_sleep (query, requis: boolean) | — | object | Cycle de vie du backend, peut faire bouger le robot | Physique + administration | `stop_daemon` | Implémenté, testé en simulation, non testé contre le daemon réel |
| POST | /api/daemon/restart | Restart Daemon | — | — | object | Cycle de vie du backend, peut faire bouger le robot | Physique + administration | `restart_daemon` | Implémenté, testé en simulation, non testé contre le daemon réel |
| GET | /api/daemon/status | Get Daemon Status | — | — | DaemonStatus | Aucun | Lecture | `get_daemon_status` | Implémenté et testé (simulation + daemon réel) |
| GET | /api/daemon/hardware-id | Get Robot Hardware Id | — | — | object | Aucun | Lecture | `get_hardware_id` | Implémenté et testé (simulation + daemon réel) |
| GET | /api/daemon/robot-app-lock-status | Get Robot App Lock Status | — | — | RobotAppLockStatus | Aucun | Lecture | `get_robot_app_lock_status` | Implémenté et testé (simulation + daemon réel) |
| POST | /api/hf-auth/save-token | Save Token | — | TokenRequest (json) | TokenResponse | — | — | — | Exclu : Would route a Hugging Face secret through the LLM context. The operator logs in through the Reachy Mini desktop app. |
| GET | /api/hf-auth/status | Get Auth Status | — | — | object | Aucun | Lecture | `get_hf_auth_status` | Implémenté et testé (simulation + daemon réel) |
| GET | /api/hf-auth/relay-status | Get Relay Status | — | — | object | Aucun | Lecture | `get_relay_status` | Implémenté et testé (simulation + daemon réel) |
| DELETE | /api/hf-auth/token | Delete Token | — | — | object | — | — | — | Exclu : Account credential management is left to the operator (desktop app). |
| POST | /api/hf-auth/refresh-relay | Refresh Relay | — | — | object | Modifie un réglage ou un fichier non physique | Configuration | `refresh_relay` | Implémenté, testé en simulation, non testé contre le daemon réel |
| GET | /api/hf-auth/central-robot-status | Get Central Robot Status | — | — | object | Aucun | Lecture | `get_central_robot_status` | Implémenté et testé (simulation + daemon réel) |
| GET | /api/hf-auth/oauth/configured | Is Oauth Configured | — | — | object | Aucun | Lecture | `get_hf_auth_status` | Implémenté et testé (simulation + daemon réel) |
| GET | /api/hf-auth/oauth/start | Start Oauth | use_localhost (query: boolean) | — | object | — | — | — | Exclu : Interactive browser OAuth flow for a human user. |
| GET | /api/hf-auth/oauth/status/{session_id} | Get Oauth Status | session_id (path, requis: string) | — | object | — | — | — | Exclu : Part of the interactive OAuth flow. |
| DELETE | /api/hf-auth/oauth/session/{session_id} | Cancel Oauth Session | session_id (path, requis: string) | — | object | — | — | — | Exclu : Part of the interactive OAuth flow. |
| GET | /api/hf-auth/oauth/callback | Oauth Callback | code (query: string \| null), state (query: string \| null), error (query: string \| null), error_description (query: string \| null) | — | any | — | — | — | Exclu : Browser redirect target of the OAuth flow; never called by clients. |
| GET | /api/kinematics/info | Get Kinematics Info | — | — | object | Aucun | Lecture | `get_kinematics_info` | Implémenté et testé (simulation + daemon réel) |
| GET | /api/kinematics/urdf | Get Urdf | — | — | object | Aucun | Lecture | `get_urdf` | Implémenté et testé (simulation + daemon réel) |
| GET | /api/kinematics/stl/{filename} | Get Stl File | filename (path, requis: string) | — | any | — | — | — | Exclu : Binary 3D mesh assets for visualisation; not useful to an LLM, and the path parameter accepts sub-paths. |
| POST | /api/media/release | Release Media | — | — | object | Cycle de vie / installation / matériel | Administration | `release_media` | Implémenté, testé en simulation, non testé contre le daemon réel |
| POST | /api/media/acquire | Acquire Media | — | — | object | Cycle de vie / installation / matériel | Administration | `acquire_media` | Implémenté, testé en simulation, non testé contre le daemon réel |
| GET | /api/media/status | Media Status | — | — | object | Aucun | Lecture | `get_media_status` | Implémenté et testé (simulation + daemon réel) |
| POST | /api/media/play_sound | Play Sound | — | PlaySoundRequest (json) | object | Mouvement, couple moteur, son ou app pilotant le robot | Physique | `play_sound`, `speak` | Implémenté et testé (simulation + daemon réel) |
| POST | /api/media/stop_sound | Stop Sound | — | — | object | Interrompt une activité | Arrêt (toujours autorisé) | `stop_sound` | Implémenté, testé en simulation, non testé contre le daemon réel |
| POST | /api/media/wobbling/enable | Enable Wobbling | — | — | object | Mouvement, couple moteur, son ou app pilotant le robot | Physique | `enable_wobbling` | Implémenté, testé en simulation, non testé contre le daemon réel |
| POST | /api/media/wobbling/disable | Disable Wobbling | — | — | object | Mouvement, couple moteur, son ou app pilotant le robot | Physique | `disable_wobbling` | Implémenté, testé en simulation, non testé contre le daemon réel |
| POST | /api/media/sounds/upload | Upload Sound | — | Body_upload_sound_api_media_sounds_upload_post (form-data) | object | Mouvement, couple moteur, son ou app pilotant le robot | Physique | `upload_sound`, `speak` | Implémenté et testé (simulation + daemon réel) |
| GET | /api/media/sounds | List Sounds | — | — | object | Aucun | Lecture | `list_uploaded_sounds` | Implémenté et testé (simulation + daemon réel) |
| DELETE | /api/media/sounds/{filename} | Delete Sound | filename (path, requis: string) | — | object | Mouvement, couple moteur, son ou app pilotant le robot | Physique | `delete_uploaded_sound`, `speak` | Implémenté et testé (simulation + daemon réel) |
| GET | /api/motors/status | Get Motor Status | — | — | MotorStatus | Aucun | Lecture | `get_motor_status`, `set_motor_mode` | Implémenté et testé (simulation + daemon réel) |
| POST | /api/motors/set_mode/{mode} | Set Motor Mode | mode (path, requis: MotorControlMode) | — | object | Mouvement, couple moteur, son ou app pilotant le robot | Physique | `set_motor_mode` | Implémenté, testé en simulation, non testé contre le daemon réel |
| GET | /api/move/running | Get Running Moves | — | — | array[MoveUUID] | Aucun | Lecture | `list_running_moves`, `get_move_status`, `wait_for_move`, `stop_all_moves` | Implémenté et testé (simulation + daemon réel) |
| POST | /api/move/goto | Goto | — | GotoModelRequest (json) | MoveUUID | Mouvement, couple moteur, son ou app pilotant le robot | Physique | `goto_pose`, `turn_toward_sound` | Implémenté et testé (simulation + daemon réel). Robot réel : mouvements exécutés. La tête n'atteint pas exactement les poses de retour (écart de 3 à 14°, voir README) ; les antennes suivent fidèlement. |
| POST | /api/move/play/wake_up | Play Wake Up | — | — | MoveUUID | Mouvement, couple moteur, son ou app pilotant le robot | Physique | `wake_up` | Implémenté, testé en simulation, non testé contre le daemon réel |
| POST | /api/move/play/goto_sleep | Play Goto Sleep | — | — | MoveUUID | Mouvement, couple moteur, son ou app pilotant le robot | Physique | `goto_sleep` | Implémenté, testé en simulation, non testé contre le daemon réel |
| GET | /api/move/recorded-move-datasets/list/{dataset_name} | List Recorded Move Dataset | dataset_name (path, requis: string) | — | array[string] | Aucun | Lecture | `list_recorded_moves` | Implémenté et testé (simulation + daemon réel) |
| POST | /api/move/play/recorded-move-dataset/{dataset_name}/{move_name} | Play Recorded Move Dataset | dataset_name (path, requis: string), move_name (path, requis: string) | — | MoveUUID | Mouvement, couple moteur, son ou app pilotant le robot | Physique | `play_recorded_move` | Implémenté, testé en simulation, non testé contre le daemon réel |
| POST | /api/move/stop | Stop Move | — | MoveUUID (json) | object | Interrompt une activité | Arrêt (toujours autorisé) | `stop_move`, `stop_all_moves` | Implémenté, testé en simulation, non testé contre le daemon réel |
| POST | /api/move/set_target | Set Target | — | FullBodyTarget (json) | object | — | — | — | Exclu : Sets a target with no duration or interpolation (instant jump), meant for high-frequency streaming control loops. goto_pose covers the same targets with a bounded duration. |
| GET | /api/state/present_head_pose | Get Head Pose | use_pose_matrix (query: boolean) | — | XYZRPYPose \\| Matrix4x4Pose | Aucun | Lecture | `get_head_pose` | Implémenté et testé (simulation + daemon réel) |
| GET | /api/state/present_body_yaw | Get Body Yaw | — | — | number | Aucun | Lecture | `get_body_yaw` | Implémenté et testé (simulation + daemon réel) |
| GET | /api/state/present_antenna_joint_positions | Get Antenna Joint Positions | — | — | array | Aucun | Lecture | `get_antenna_positions` | Implémenté et testé (simulation + daemon réel) |
| GET | /api/state/doa | Get Doa | — | — | DoAInfo \\| null | Aucun | Lecture | `get_direction_of_arrival`, `listen`, `turn_toward_sound` | Implémenté et testé (simulation + daemon réel) |
| GET | /api/state/full | Get Full State | with_control_mode (query: boolean), with_head_pose (query: boolean), with_target_head_pose (query: boolean), with_head_joints (query: boolean), with_target_head_joints (query: boolean), with_body_yaw (query: boolean), with_target_body_yaw (query: boolean), with_antenna_positions (query: boolean), with_target_antenna_positions (query: boolean), with_passive_joints (query: boolean), with_doa (query: boolean), use_pose_matrix (query: boolean) | — | FullState | Aucun | Lecture | `get_full_state` | Implémenté et testé (simulation + daemon réel) |
| GET | /api/volume/current | Get Volume | — | — | VolumeResponse | Aucun | Lecture | `get_volume` | Implémenté et testé (simulation + daemon réel) |
| POST | /api/volume/set | Set Volume | — | VolumeRequest (json) | VolumeResponse | Mouvement, couple moteur, son ou app pilotant le robot | Physique | `set_volume` | Implémenté, testé en simulation, non testé contre le daemon réel |
| POST | /api/volume/test-sound | Play Test Sound | — | — | TestSoundResponse | Mouvement, couple moteur, son ou app pilotant le robot | Physique | `play_test_sound` | Implémenté, testé en simulation, non testé contre le daemon réel |
| GET | /api/volume/microphone/current | Get Microphone Volume | — | — | VolumeResponse | Aucun | Lecture | `get_microphone_volume` | Implémenté et testé (simulation + daemon réel) |
| POST | /api/volume/microphone/set | Set Microphone Volume | — | VolumeRequest (json) | VolumeResponse | Modifie un réglage ou un fichier non physique | Configuration | `set_microphone_volume` | Implémenté, testé en simulation, non testé contre le daemon réel |
| POST | /health-check | Health Check | — | — | object | — | — | — | Exclu : Resets the daemon's own watchdog timer used by the desktop app; calling it from MCP would mask a dead desktop client. |
| GET | / | Dashboard | — | — | any | — | — | — | Exclu : HTML dashboard for humans; no machine-readable data. |

## Outils sans endpoint direct

* `diagnose_connectivity` : diagnostic réseau en 4 étapes (DNS, TCP, HTTP, état du daemon).
* `get_server_safety_config` : configuration de sécurité du serveur MCP.
* `get_perception_status` : état du flux WebRTC et des modèles vocaux locaux.
* `capture_image` : image de la caméra, reçue par le flux WebRTC du daemon (port 8443, non décrit dans l'OpenAPI). Testé sur le robot réel.

## Flux média hors OpenAPI

Le daemon exécute aussi un serveur média GStreamer `webrtcsink` avec son serveur de signalisation (`ws://<hôte>:8443`, producteur `reachymini`). Il diffuse la vidéo de la caméra et l'audio des micros. Ce flux n'apparaît pas dans l'OpenAPI ; il a été identifié dans le code du daemon (`reachy_mini/media/media_server.py`) et validé en réel depuis le conteneur. Il alimente `capture_image` et `listen`. La reconnaissance vocale (faster-whisper) et la synthèse vocale (Piper) tournent localement dans le conteneur.
