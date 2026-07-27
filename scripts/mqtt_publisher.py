"""
mqtt_publisher.py
-----------------
Minimal MQTT publishing for the (optional) Home Assistant integration.

The first, deliberately tiny step of Feature #3: publish the running app
version to an MQTT broker so it shows up in MQTT Explorer / Home Assistant.
Built on paho-mqtt (installed by bootstrap.ps1). The import is lazy so the GUI
still loads on an older install whose venv predates paho-mqtt.

Config lives in config.json under "mqtt":
    enabled, host, port, username, password, client_id

Network calls block; call them from a background thread (the GUI does).
"""

import threading

# Fail-safe diagnostic trail for the swallowed-error handlers (guarded import).
try:
    from debug_log import debug_log
except Exception:
    def debug_log(*_a, **_k):
        pass

DEFAULT_CLIENT_ID = "image-toolbox-beededbe"
BASE_TOPIC        = "image-toolbox"

VERSION_TOPIC        = f"{BASE_TOPIC}/version"
UPDATE_TOPIC         = f"{BASE_TOPIC}/update"
LATEST_VERSION_TOPIC = f"{BASE_TOPIC}/latest_version"
AVAILABILITY_TOPIC   = f"{BASE_TOPIC}/availability"
LAST_RUN_TOPIC       = f"{BASE_TOPIC}/last_run"
LAST_USED_TOPIC      = f"{BASE_TOPIC}/last_used"

# System telemetry (published on a timer while a task runs — Feature #3a)
SYS_CPU_TOPIC        = f"{BASE_TOPIC}/system/cpu"               # CPU usage %
SYS_RAM_TOPIC        = f"{BASE_TOPIC}/system/ram"             # RAM used, MB
SYS_RAM_TOTAL_TOPIC  = f"{BASE_TOPIC}/system/ram_total"       # RAM total, MB
SYS_GPU_VRAM_TOPIC   = f"{BASE_TOPIC}/system/gpu_vram"         # VRAM used, MB
SYS_GPU_VRAM_TOTAL_TOPIC = f"{BASE_TOPIC}/system/gpu_vram_total"  # VRAM total, MB
SYS_GPU_TEMP_TOPIC   = f"{BASE_TOPIC}/system/gpu_temp"         # GPU temp, °C
SYS_GPU_UTIL_TOPIC   = f"{BASE_TOPIC}/system/gpu_util"          # GPU core load, %
SYS_GPU_POWER_TOPIC  = f"{BASE_TOPIC}/system/gpu_power"         # GPU power draw, W
SYS_GPU_POWER_LIMIT_TOPIC = f"{BASE_TOPIC}/system/gpu_power_limit"  # GPU power cap, W
SYS_GPU_CLOCK_TOPIC  = f"{BASE_TOPIC}/system/gpu_clock"         # GPU core clock, MHz

# Remote-pod system telemetry (remote upscaling #1, Feature #4) — published only
# while a remote-pod run is active; otherwise these topics are absent/stale.
SYS_REMOTE_CPU_TOPIC        = f"{BASE_TOPIC}/system/remote/cpu"            # CPU %
SYS_REMOTE_RAM_TOPIC        = f"{BASE_TOPIC}/system/remote/ram"           # RAM used, MB
SYS_REMOTE_RAM_TOTAL_TOPIC  = f"{BASE_TOPIC}/system/remote/ram_total"     # RAM total, MB
SYS_REMOTE_GPU_VRAM_TOPIC   = f"{BASE_TOPIC}/system/remote/gpu_vram"      # VRAM used, MB
SYS_REMOTE_GPU_VRAM_TOTAL_TOPIC = f"{BASE_TOPIC}/system/remote/gpu_vram_total"  # VRAM total, MB
SYS_REMOTE_GPU_TEMP_TOPIC   = f"{BASE_TOPIC}/system/remote/gpu_temp"      # GPU temp, °C
SYS_REMOTE_GPU_UTIL_TOPIC   = f"{BASE_TOPIC}/system/remote/gpu_util"      # GPU core load, %
SYS_REMOTE_GPU_POWER_TOPIC  = f"{BASE_TOPIC}/system/remote/gpu_power"     # GPU power draw, W
SYS_REMOTE_GPU_POWER_LIMIT_TOPIC = f"{BASE_TOPIC}/system/remote/gpu_power_limit"  # GPU power cap, W
SYS_REMOTE_GPU_CLOCK_TOPIC  = f"{BASE_TOPIC}/system/remote/gpu_clock"     # GPU core clock, MHz

# Live task state (published by the persistent client during a run)
TASK_NAME_TOPIC     = f"{BASE_TOPIC}/task/name"        # idle / upscaling / tagging / conciliating
TASK_DETAILS_TOPIC  = f"{BASE_TOPIC}/task/details"     # human-readable phase line
TASK_RUNTIME_TOPIC  = f"{BASE_TOPIC}/task/runtime"     # seconds of active-task runtime
TASK_PROGRESS_TOPIC = f"{BASE_TOPIC}/task/progress"    # "X/Y"
TASK_ETA_TOPIC      = f"{BASE_TOPIC}/task/eta"         # estimated time remaining
TASK_AVG_TOPIC      = f"{BASE_TOPIC}/task/average_processing_time"   # seconds/image
TASK_LAST_TOPIC     = f"{BASE_TOPIC}/task/last_processing_time"      # seconds for last image

# ── Events (0.5.8): NOT retained, unlike every topic above ───────────────────
# Everything else here is retained STATE ("what is true now"), which a subscriber
# is re-sent the moment it subscribes: the whole point, so a dashboard is correct
# after a restart. That makes retained state a **bad trigger**: an automation on
# "last_run changed" fires again on every Home Assistant restart and every broker
# reconnect, re-announcing a run that finished days ago. These two are the other
# half of the pattern: one-shot EVENTS, published non-retained (and never stored
# for the reconnect replay), so they are delivered live exactly once and cannot be
# replayed. Trigger on these; read the retained topics for state. Payload is the
# same JSON object as `last_run` (run_finished) / `{tool, started_at}` (run_started).
EVENT_RUN_STARTED_TOPIC  = f"{BASE_TOPIC}/event/run_started"
EVENT_RUN_FINISHED_TOPIC = f"{BASE_TOPIC}/event/run_finished"

ONLINE  = "online"
OFFLINE = "offline"


def mqtt_available():
    """True if paho-mqtt is importable in this environment."""
    try:
        import paho.mqtt  # noqa: F401
        return True
    except Exception:
        return False


def _auth(mqtt_cfg):
    user = (mqtt_cfg.get("username") or "").strip()
    if not user:
        return None
    return {"username": user, "password": mqtt_cfg.get("password") or ""}


def test_connection(mqtt_cfg, timeout=10):
    """
    Connect to the broker with the given settings (and authenticate, if a
    username is set), then disconnect — without publishing anything. Returns
    (ok, message). Distinguishes "can't reach the host" from "broker refused
    the credentials".
    """
    host = (mqtt_cfg.get("host") or "").strip()
    if not host:
        return False, "No MQTT host is configured."
    try:
        port = int(mqtt_cfg.get("port") or 1883)
    except (TypeError, ValueError):
        return False, "MQTT port must be a number."

    try:
        import paho.mqtt.client as mqtt
    except Exception:
        return False, ("paho-mqtt is not installed. Re-run the installer (or "
                       "'pip install paho-mqtt' in the app's .venv) to enable MQTT.")

    client_id = (mqtt_cfg.get("client_id") or "").strip() or DEFAULT_CLIENT_ID
    user      = (mqtt_cfg.get("username") or "").strip()

    connected = threading.Event()
    outcome   = {}

    def on_connect(client, userdata, flags, reason_code, properties=None):
        outcome["rc"] = reason_code
        connected.set()

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except Exception:
        client = mqtt.Client(client_id=client_id)   # paho 1.x fallback
    if user:
        client.username_pw_set(user, mqtt_cfg.get("password") or "")
    client.on_connect = on_connect

    try:
        client.connect(host, port, keepalive=timeout)
        client.loop_start()
        if not connected.wait(timeout):
            return False, f"Timed out connecting to {host}:{port}."
        rc = outcome.get("rc")
        # paho 2.x hands back a ReasonCode (has .is_failure); 1.x hands back 0/int.
        failed = getattr(rc, "is_failure", None)
        if failed is True or (failed is None and rc not in (0, None)):
            return False, f"Broker refused the connection: {rc}."
        return True, f"Connected to {host}:{port} successfully."
    except Exception as exc:
        return False, f"Could not connect: {exc}"
    finally:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass


def publish_state(mqtt_cfg, values, timeout=10):
    """
    Publish a set of {topic: payload} pairs to the broker in a single connection,
    each retained (so late subscribers like Home Assistant get the last value).
    Returns (ok, message). A connect/publish/disconnect one-shot — fine for the
    startup snapshot of values; live runtime state will later use a persistent
    client (needed for an availability/LWT topic).
    """
    host = (mqtt_cfg.get("host") or "").strip()
    if not host:
        return False, "No MQTT host is configured."
    try:
        port = int(mqtt_cfg.get("port") or 1883)
    except (TypeError, ValueError):
        return False, "MQTT port must be a number."
    if not values:
        return False, "Nothing to publish."

    try:
        import paho.mqtt.publish as publish
    except Exception:
        return False, ("paho-mqtt is not installed. Re-run the installer (or "
                       "'pip install paho-mqtt' in the app's .venv) to enable MQTT.")

    client_id = (mqtt_cfg.get("client_id") or "").strip() or DEFAULT_CLIENT_ID
    msgs = [{"topic": topic, "payload": str(payload), "retain": True, "qos": 0}
            for topic, payload in values.items()]
    try:
        publish.multiple(
            msgs,
            hostname=host,
            port=port,
            client_id=client_id,
            auth=_auth(mqtt_cfg),
            keepalive=timeout,
        )
        return True, f"Published {len(msgs)} topic(s) to {host}:{port}."
    except Exception as exc:
        return False, f"MQTT publish failed: {exc}"


def publish_version(mqtt_cfg, version, timeout=10):
    """Convenience: publish just the version topic."""
    return publish_state(mqtt_cfg, {VERSION_TOPIC: str(version)}, timeout)


class MqttClient:
    """
    Persistent MQTT connection for live state.

    Stays connected for the app's lifetime (auto-reconnecting), so it can publish
    runtime task state as it changes and — crucially — set an availability
    Last-Will-and-Testament: the broker publishes 'offline' to the availability
    topic if the app dies without a clean disconnect, so Home Assistant always
    knows whether the app is actually running.

    Retained publishes are remembered and replayed on (re)connect, so a value
    published before the link is up (or across a reconnect) is not lost.
    """

    def __init__(self, mqtt_cfg, status_cb=None):
        self.cfg        = dict(mqtt_cfg)
        self._client    = None
        self._connected = False
        self._lock      = threading.Lock()
        self._retained  = {}          # topic -> last retained payload
        # Optional callback(connected: bool, message: str), invoked from the
        # network thread when the connection state changes (the GUI marshals it
        # to the UI thread). Lets the app verify/report connectivity.
        self._status_cb = status_cb

    def start(self):
        """Connect (async, with auto-reconnect). Returns (ok, message)."""
        host = (self.cfg.get("host") or "").strip()
        if not host:
            return False, "No MQTT host is configured."
        try:
            port = int(self.cfg.get("port") or 1883)
        except (TypeError, ValueError):
            return False, "MQTT port must be a number."
        try:
            import paho.mqtt.client as mqtt
        except Exception:
            return False, "paho-mqtt is not installed."

        client_id = (self.cfg.get("client_id") or "").strip() or DEFAULT_CLIENT_ID
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                 client_id=client_id, clean_session=True)
        except Exception:
            client = mqtt.Client(client_id=client_id)   # paho 1.x fallback

        user = (self.cfg.get("username") or "").strip()
        if user:
            client.username_pw_set(user, self.cfg.get("password") or "")
        client.will_set(AVAILABILITY_TOPIC, OFFLINE, qos=1, retain=True)
        client.on_connect    = self._on_connect
        client.on_disconnect = self._on_disconnect
        self._client = client
        try:
            client.connect_async(host, port, keepalive=60)
            client.loop_start()
            return True, f"MQTT client connecting to {host}:{port}."
        except Exception as exc:
            self._client = None
            return False, f"MQTT start failed: {exc}"

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            self._notify(False, f"Broker refused the connection: {reason_code}.")
            return
        self._connected = True
        # Announce availability and replay every retained value we hold, so a
        # fresh broker (or one that lost our session) is brought up to date.
        client.publish(AVAILABILITY_TOPIC, ONLINE, qos=1, retain=True)
        with self._lock:
            items = list(self._retained.items())
        for topic, payload in items:
            client.publish(topic, payload, qos=0, retain=True)
        host = (self.cfg.get("host") or "").strip()
        self._notify(True, f"Connected to {host}:{self.cfg.get('port', 1883)}.")

    def _on_disconnect(self, *args):
        was_connected = self._connected
        self._connected = False
        if was_connected:
            self._notify(False, "Disconnected from the broker (will retry).")

    def _notify(self, connected, message):
        if self._status_cb is not None:
            try:
                self._status_cb(connected, message)
            except Exception:
                pass

    def publish(self, topic, payload, retain=True, qos=0):
        """Publish one topic (best-effort). Retained values are remembered so a
        reconnect can replay them."""
        payload = "" if payload is None else str(payload)
        if retain:
            with self._lock:
                self._retained[topic] = payload
        client = self._client
        if client is None:
            return
        try:
            client.publish(topic, payload, qos=qos, retain=retain)
            self._publish_broken = False
        except Exception as exc:
            # Publishes can fire every few seconds, so log the first failure of a
            # broken streak only (reset on the next success) to avoid flooding
            # debug.log while the broker is down.
            if not getattr(self, "_publish_broken", False):
                self._publish_broken = True
                debug_log("MqttClient.publish (silenced until recovery)", exc=exc)

    def publish_many(self, values, retain=True, qos=0):
        """Publish a {topic: payload} mapping. `retain=False` marks the batch as
        one-shot EVENTS: they are neither retained by the broker nor remembered for
        the reconnect replay, so no subscriber can ever be handed them twice (see
        the EVENT_* topics). Events go out at qos=1 by default at the call site,
        since there is no retained copy to fall back on."""
        for topic, payload in values.items():
            self.publish(topic, payload, retain=retain, qos=qos)

    def stop(self, last_used=None):
        """Publish a clean 'offline' (and optional last_used), then disconnect.

        The teardown runs on a daemon thread with a BOUNDED join so it can never
        freeze the caller: `loop_stop()` joins paho's network thread, and that join
        can stall on the UI thread regardless of broker health (a healthy broker
        was confirmed when this froze on Save/exit). This is called from the GUI
        thread (restart_mqtt / _on_close). If the teardown can't finish quickly the
        thread is abandoned (daemon, dies with the process) and the caller
        proceeds; the broker still delivers the LWT 'offline' when the dropped
        socket times out, so nothing is lost."""
        client = self._client
        self._client = None
        self._connected = False
        if client is None:
            return

        def _teardown():
            try:
                if last_used is not None:
                    client.publish(LAST_USED_TOPIC, str(last_used), qos=1, retain=True)
                client.publish(AVAILABILITY_TOPIC, OFFLINE, qos=1, retain=True)
                import time as _time
                _time.sleep(0.3)        # let the network loop flush before stopping
                # disconnect() BEFORE loop_stop(): closing the socket wakes the
                # network loop's select immediately, so loop_stop()'s thread-join
                # returns at once instead of waiting out paho's ~1 s loop timeout
                # (the join, on the UI thread, was the real Save/exit stall — not
                # broker reachability, which stays fine).
                client.disconnect()
                client.loop_stop()
            except Exception:
                pass

        t = threading.Thread(target=_teardown, name="mqtt-stop", daemon=True)
        t.start()
        t.join(timeout=2.0)            # never block the UI thread on a dead broker
