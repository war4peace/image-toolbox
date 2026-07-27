"""
Guards for the Home Assistant samples in samples/home-assistant/.

A broken sample is worse than no sample: the user pastes it into their own
configuration, gets a YAML or Jinja error at reload, and has no way to tell
whether they made the mistake or we shipped it. Nothing else in the repo reads
these files, so nothing else would ever notice.

What is checked:
  * Every sample parses, and every Jinja template in them compiles.
  * The MQTT topics the samples subscribe to are the ones the app actually
    publishes (they are constants in mqtt_publisher, so a rename is caught here
    instead of silently killing an automation).
  * Every entity the automations and the two dashboards reference is one the
    sensor samples define (mqtt-sensors.yaml or template-sensors.yaml). A
    dashboard naming an entity nobody defines renders as an "Entity not
    available" card, which looks like the user's mistake.
  * The F2 guards are present: the automation that triggers on RETAINED state has
    both its startup guard and its freshness guard, and the Last-Will automation
    triggers only on a real online -> offline transition.

yaml/jinja2 are dev-only here (the app itself needs neither), so the module skips
if they are absent.
"""

import os
import re

import pytest

yaml = pytest.importorskip("yaml")
jinja2 = pytest.importorskip("jinja2")

import mqtt_publisher as mp        # noqa: E402

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "samples", "home-assistant")


def _load(name):
    with open(os.path.join(SAMPLES, name), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _load_all(name):
    """The automations sample is a stream of one-automation documents."""
    with open(os.path.join(SAMPLES, name), encoding="utf-8") as fh:
        return [d for d in yaml.safe_load_all(fh) if d]


@pytest.fixture(scope="module")
def automations():
    return _load_all("automations-ui.yaml")


@pytest.fixture(scope="module")
def sensors():
    return _load("mqtt-sensors.yaml")


@pytest.fixture(scope="module")
def template_sensors():
    return _load("template-sensors.yaml")["template"][0]["sensor"]


@pytest.fixture(scope="module", params=["dashboard-core.yaml", "dashboard-custom.yaml"])
def dashboard(request):
    return request.param, _load(request.param)


def _walk(node):
    """Every (path, value) leaf, so a check can sweep the whole document."""
    stack = [("", node)]
    while stack:
        path, cur = stack.pop()
        if isinstance(cur, dict):
            stack += [(f"{path}.{k}", v) for k, v in cur.items()]
        elif isinstance(cur, list):
            stack += [(f"{path}[{i}]", v) for i, v in enumerate(cur)]
        else:
            yield path, cur


def _entity_id(sensor_name):
    """Home Assistant's slug for an MQTT sensor's default entity id."""
    return "sensor." + re.sub(r"[^a-z0-9]+", "_", sensor_name.lower()).strip("_")


# ── it parses, and it is what it claims to be ────────────────────────────────

def test_each_automation_is_one_standalone_document(automations):
    """The sample is pasted into the UI's "Edit in YAML" editor, which holds ONE
    automation and rejects anything else with "extra keys not allowed @ data['0']".
    So each document must be a bare mapping (never a list), with no `id` (the UI
    assigns its own) and no `initial_state` (file-only; the ones that should start
    off say to switch them off after saving)."""
    assert automations, "no automation documents parsed: are the `---` separators intact?"
    for a in automations:
        assert isinstance(a, dict), f"{a!r} is not a single automation mapping"
        assert "id" not in a, f"{a['alias']}: `id` is assigned by the UI"
        assert "initial_state" not in a, f"{a['alias']}: `initial_state` is file-only"
        assert a["alias"] and a["triggers"] and a["actions"]
    aliases = [a["alias"] for a in automations]
    assert len(set(aliases)) == len(aliases)      # the alias is how the user tells them apart


def test_every_jinja_template_compiles(automations, sensors):
    env = jinja2.Environment()
    seen = 0
    for doc in (automations, sensors):
        for path, value in _walk(doc):
            if isinstance(value, str) and ("{{" in value or "{%" in value):
                seen += 1
                env.parse(value)                  # raises on a typo
    assert seen > 10                              # the sweep actually found them


# ── the samples and the app agree on the wire ────────────────────────────────

def test_subscribed_topics_are_topics_the_app_publishes(automations):
    published = {v for k, v in vars(mp).items()
                 if k.endswith("_TOPIC") and isinstance(v, str)}
    topics = [t["topic"] for a in automations for t in a["triggers"]
              if t.get("trigger") == "mqtt"]
    assert topics, "no mqtt triggers left in the sample?"
    for topic in topics:
        assert topic in published, f"{topic} is not published by mqtt_publisher"


def test_sensor_topics_are_topics_the_app_publishes(sensors):
    published = {v for k, v in vars(mp).items()
                 if k.endswith("_TOPIC") and isinstance(v, str)}
    for s in sensors:
        assert s["state_topic"] in published, s["state_topic"]


def _referenced_entities(doc):
    used = set()
    for path, value in _walk(doc):
        if path.endswith(".entity_id") or path.endswith(".entity"):
            used.add(value)
        elif isinstance(value, str):
            used |= set(re.findall(r"sensor\.image_toolbox_[a-z0-9_]+", value))
    return {e for e in used if isinstance(e, str) and e.startswith("sensor.image_toolbox")}


def test_every_referenced_entity_is_defined_by_the_sensor_sample(automations, sensors):
    defined = {_entity_id(s["name"]) for s in sensors}
    used = _referenced_entities(automations)
    assert used, "the automations reference no entities at all?"
    assert used <= defined, f"undefined entities: {sorted(used - defined)}"


def test_dashboards_parse_and_reference_only_defined_entities(
        dashboard, sensors, template_sensors):
    """A dashboard card is pasted verbatim, so a typo'd entity shows the user an
    'Entity not available' box that looks like their own mistake. Both sample
    files must resolve entirely against the two sensor samples."""
    name, doc = dashboard
    assert doc["type"] == "vertical-stack" and doc["cards"], name
    defined = {_entity_id(s["name"]) for s in sensors}
    defined |= {_entity_id(s["name"]) for s in template_sensors}
    used = _referenced_entities(doc)
    assert used, f"{name} references no entities at all?"
    assert used <= defined, f"{name}: undefined entities: {sorted(used - defined)}"


def test_dashboard_templates_compile(dashboard):
    name, doc = dashboard
    env = jinja2.Environment()
    for path, value in _walk(doc):
        if isinstance(value, str) and ("{{" in value or "{%" in value):
            env.parse(value)                      # raises on a typo


def test_the_progress_percent_sensor_exists_for_the_dashboards(template_sensors):
    """0.5.8: both dashboards gained a progress gauge that reads it. It is a
    derived sensor, so nothing in the app would notice if it were dropped."""
    by_id = {s["unique_id"]: s for s in template_sensors}
    sensor = by_id["image_toolbox_task_progress_percent"]
    assert sensor["unit_of_measurement"] == "%"
    assert "sensor.image_toolbox_task_progress" in sensor["state"]


@pytest.mark.parametrize("progress,expected", [
    ("37/100", "37"),
    ("8412/95160", "9"),          # a video run: frames, so the raw text is unreadable
    ("0/0", "0"),                 # a run that found nothing: no division by zero
    ("unknown", "0"),             # idle / before the first publish
    ("", "0"),
    ("12", "0"),                  # not "X/Y" at all
])
def test_the_progress_percent_template_renders(template_sensors, progress, expected):
    """Rendered against the real states, because the failure mode is a template
    error in the user's log plus a sensor stuck at `unavailable` -- with no hint
    that the sample, not their setup, is at fault."""
    src = next(s for s in template_sensors
               if s["unique_id"] == "image_toolbox_task_progress_percent")["state"]
    env = jinja2.Environment()
    rendered = env.from_string(src).render(states=lambda _: progress).strip()
    assert rendered == expected, rendered


# ── the webhook receiver (the no-broker route, 0.5.8) ────────────────────────

@pytest.fixture(scope="module")
def webhook_automation():
    return _load("automation-webhook.yaml")


def test_the_webhook_sample_is_one_pasteable_automation(webhook_automation):
    a = webhook_automation
    assert isinstance(a, dict) and "id" not in a and "initial_state" not in a
    assert a["alias"] and a["actions"]


def test_the_webhook_trigger_is_locked_to_the_local_network(webhook_automation):
    """The webhook id is the endpoint's ONLY credential, so `local_only` is what
    keeps a guessed id from being fired from the internet. It is also HA's default,
    and stated explicitly here so the sample cannot silently lose it."""
    trig = webhook_automation["triggers"][0]
    assert trig["trigger"] == "webhook"
    assert trig["local_only"] is True
    assert trig["allowed_methods"] == ["POST"]      # what the app sends


def test_the_webhook_sample_tells_the_user_to_change_the_id(webhook_automation):
    """Shipping a usable default id would mean every install on a LAN shares one
    guessable endpoint."""
    assert "CHANGE_ME" in webhook_automation["triggers"][0]["webhook_id"]


def test_the_webhook_sample_renders_against_the_real_payload():
    """The sample reads `trigger.json.*`; the app builds that dict in
    notifications.ha_payload. Renaming a key there would leave every user's
    automation quietly filling in blanks, and nothing else would notice."""
    import notifications

    payload = notifications.ha_payload(
        "Upscale finished", "37 processed, 2 skipped", notifications.COLOR_GREEN,
        [{"name": "Source", "value": "X:\\Poze"}], source="Upscale Bot")
    doc = _load("automation-webhook.yaml")
    env = jinja2.Environment()
    trigger = type("T", (), {"json": payload})
    rendered = {k: env.from_string(v).render(trigger=trigger)
                for k, v in doc["actions"][0]["data"].items()}
    assert rendered["title"] == "Upscale finished"
    assert "37 processed" in rendered["message"]
    assert "Source: X:\\Poze" in rendered["message"]     # the pre-rendered body
    for value in rendered.values():
        assert "no details" not in value                 # the default() fallbacks
        assert "Undefined" not in value


def test_the_documented_field_lookup_works_on_the_real_payload():
    """The "pick one detail" recipe in the sample's comments. Field names contain
    spaces, so it is a selectattr rather than an attribute access; if that recipe
    is wrong the user gets an empty string and no error."""
    import notifications

    payload = notifications.ha_payload(
        "T", "D", notifications.COLOR_GREEN,
        [{"name": "Source", "value": "X:\\Poze"}, {"name": "Machine", "value": "DESKTOP"}])
    tmpl = ("{{ trigger.json.fields | selectattr('name', 'eq', 'Machine') "
            "| map(attribute='value') | first | default('') }}")
    trigger = type("T", (), {"json": payload})
    assert jinja2.Environment().from_string(tmpl).render(trigger=trigger) == "DESKTOP"


# ── finding F2: the retained-topic guards ────────────────────────────────────

RUN_FINISHED = "Image Toolbox - run finished"
RUN_PROBLEMS = "Image Toolbox - run finished with problems"
RETAINED_ROUTE = "Image Toolbox - run finished (retained-sensor route)"
OFFLINE_MIDRUN = "Image Toolbox - app went offline mid-run"


def _by_alias(automations, wanted):
    return next(a for a in automations if a["alias"] == wanted)


def test_the_recommended_route_triggers_on_the_non_retained_event(automations):
    """The "run finished" automation must use the event topic. A retained topic
    would re-announce an old run on every HA restart."""
    a = _by_alias(automations, RUN_FINISHED)
    assert [t["topic"] for t in a["triggers"]] == [mp.EVENT_RUN_FINISHED_TOPIC]


def test_the_retained_route_carries_both_guards(automations):
    """The alternative route triggers on retained state, so it needs the startup
    guard (unknown/unavailable) AND the freshness guard on `finished_at`, or it
    fires on every restart."""
    a = _by_alias(automations, RETAINED_ROUTE)
    trig = a["triggers"][0]
    assert "unknown" in trig["not_from"] and "unavailable" in trig["not_from"]
    guard = a["conditions"][0]["value_template"]
    assert "finished_at" in guard and "as_timestamp" in guard


def test_the_retained_route_warns_against_adding_it_as_well():
    """It duplicates the "run finished" alert, so with both in place every
    notification arrives twice. A file-pasted automation could ship disabled
    (`initial_state: false`); one built in the UI cannot, so this warning in its
    banner is the only thing standing between the user and double alerts. Checked
    in the raw text because it lives in a comment."""
    with open(os.path.join(SAMPLES, "automations-ui.yaml"), encoding="utf-8") as fh:
        src = fh.read()
    banner = src.split(RETAINED_ROUTE)[0].rsplit("# ===", 2)[-2]
    assert "ONLY INSTEAD OF BLOCK 1" in banner, banner


def test_the_last_will_automation_only_fires_on_a_real_transition(automations):
    """`from: online` is the guard: at HA startup the sensor goes
    unknown -> offline, which would otherwise alert on every restart while the
    app happens to be closed."""
    trig = _by_alias(automations, OFFLINE_MIDRUN)["triggers"][0]
    assert trig["from"] == "online" and trig["to"] == "offline"


# ── the templates actually work on every runner's payload ────────────────────
#
# One automation is supposed to cover all four tools, and the four runners do NOT
# emit identical keys. Rendering the real templates against a real payload from
# each is the only way to know the `default(...)` fallbacks hold.

# The DONE payloads as the four runners emit them (batch_upscale.py:1918,
# tag_and_rename.py:2246, conciliate.py:616, batch_video_upscale._done_payload),
# after MqttTaskState adds `tool`/`finished_at`.
_PAYLOADS = {
    "upscale-clean": {
        "tool": "upscale", "processed": 37, "skipped": 2, "corrupt": 0, "failed": 0,
        "elapsed_seconds": 4353.0, "stopped_by_user": False, "degraded": False,
        "remote_stopped": False, "finished_at": "2026-07-27T18:04:11+03:00"},
    "upscale-degraded": {
        "tool": "upscale", "processed": 9, "skipped": 0, "corrupt": 0, "failed": 0,
        "elapsed_seconds": 900.0, "stopped_by_user": False, "degraded": True,
        "remote_stopped": False, "finished_at": "2026-07-27T18:04:11+03:00"},
    "upscale-user-stop": {
        "tool": "upscale", "processed": 4, "skipped": 0, "corrupt": 0, "failed": 0,
        "elapsed_seconds": 120.0, "stopped_by_user": True, "degraded": False,
        "remote_stopped": False, "finished_at": "2026-07-27T18:04:11+03:00"},
    "tag-clean": {
        "tool": "tag", "processed": 12, "rotated": 3, "skipped": 1, "failed": 0,
        "elapsed_seconds": 90.0, "stop_reason": "completed",
        "finished_at": "2026-07-27T18:04:11+03:00"},
    "tag-failed": {
        "tool": "tag", "processed": 12, "rotated": 3, "skipped": 1, "failed": 2,
        "elapsed_seconds": 90.0, "stop_reason": "completed",
        "finished_at": "2026-07-27T18:04:11+03:00"},
    "conciliate-clean": {
        "tool": "conciliating", "done": 5, "conflicts": 0, "errors": 0,
        "removed_dirs": 1, "processed": 5, "failed": 0, "elapsed_seconds": 3.2,
        "stopped_by_user": False, "finished_at": "2026-07-27T18:04:11+03:00"},
    "video-clean": {
        "tool": "video", "processed": 3, "failed": 0, "total": 3, "files": 3,
        "elapsed_seconds": 7321.0, "stop_reason": "completed",
        "stopped_by_user": False, "cost": 2.47, "source": "X:\\Video",
        "finished_at": "2026-07-27T18:04:11+03:00"},
    "video-capped": {
        "tool": "video", "processed": 1, "failed": 0, "total": 1, "files": 1,
        "elapsed_seconds": 3600.0, "stop_reason": "per-run cap of 60 min reached",
        "stopped_by_user": False, "cost": 1.2, "source": "X:\\Video",
        "finished_at": "2026-07-27T18:04:11+03:00"},
}

_CLEAN = {"upscale-clean", "tag-clean", "conciliate-clean", "video-clean"}


def _render(template, payload):
    env = jinja2.Environment()
    trigger = type("T", (), {"payload_json": payload})
    return env.from_string(template).render(trigger=trigger).strip()


def test_the_two_finished_automations_partition_every_runners_payload(automations):
    """Both listen to the same event, so exactly ONE must accept each run: a run
    that satisfies neither is silent, a run that satisfies both notifies twice."""
    clean = _by_alias(automations, RUN_FINISHED)["conditions"][0]
    problem = _by_alias(automations, RUN_PROBLEMS)["conditions"][0]
    for name, payload in _PAYLOADS.items():
        got_clean = _render(clean["value_template"], payload) == "True"
        got_problem = _render(problem["value_template"], payload) == "True"
        assert got_clean != got_problem, f"{name}: both/neither fired"
        assert got_clean is (name in _CLEAN), name


def test_the_finished_message_reads_correctly_for_every_tool(automations):
    """The `default(...)` fallbacks have to hold across four runners that emit
    different keys — a stray 'None' or a raised UndefinedError lands in the user's
    notification."""
    a = _by_alias(automations, RUN_FINISHED)
    title = a["actions"][0]["data"]["title"]
    message = a["actions"][0]["data"]["message"]
    for name in _CLEAN:
        payload = _PAYLOADS[name]
        rendered = f"{_render(title, payload)} | {_render(message, payload)}"
        assert "None" not in rendered and "Undefined" not in rendered, rendered
        assert str(payload["processed"]) in rendered, rendered
    # The one video-only extra: a billed pod's cost is quoted, and only then.
    assert "$2.47" in _render(message, _PAYLOADS["video-clean"])
    assert "$" not in _render(message, _PAYLOADS["tag-clean"])


def test_the_problem_message_names_the_reason(automations):
    message = _by_alias(automations, RUN_PROBLEMS)["actions"][0]["data"]["message"]
    assert "GPU slowed down" in _render(message, _PAYLOADS["upscale-degraded"])
    assert "per-run cap" in _render(message, _PAYLOADS["video-capped"])
    assert "You stopped it" in _render(message, _PAYLOADS["upscale-user-stop"])
    assert "2 failed" in _render(message, _PAYLOADS["tag-failed"])
