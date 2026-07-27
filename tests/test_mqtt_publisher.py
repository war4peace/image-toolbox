"""
Tests for mqtt_publisher's retained-vs-event publishing (finding F2 of
docs/coarse-ideas-plan.md).

The trap: every state topic is published **retained**, and `MqttClient` also
replays its retained set on each (re)connect. A Home Assistant automation that
triggers on a retained topic therefore fires on the message it is handed when it
subscribes - so "notify me when a run finishes" re-fires on every HA restart and
every broker reconnect, re-announcing a run that finished days ago.

The fix is the standard MQTT split: retained topics for STATE, non-retained
topics for EVENTS. These tests pin the mechanism, since the value of an event
topic is entirely in flags a reader can't see at the call site.

paho-mqtt is never needed here: `MqttClient` is exercised with no live client
(`_client is None`), which is exactly the "publish before the link is up" path,
and `_on_connect` is driven with a fake client that records its publishes.
"""

import mqtt_publisher as mp


class _FakeClient:
    """Records what _on_connect replays. Mirrors paho's publish signature."""

    def __init__(self):
        self.calls = []          # [(topic, payload, qos, retain)]

    def publish(self, topic, payload, qos=0, retain=False):
        self.calls.append((topic, payload, qos, retain))

    def topics(self):
        return [t for t, _p, _q, _r in self.calls]


def _client():
    return mp.MqttClient({"host": "broker.example", "port": 1883})


# ── the topics themselves ────────────────────────────────────────────────────

def test_event_topics_live_under_their_own_prefix():
    """Separate branch, so a user (or a wildcard subscription) can tell state from
    events at a glance: image-toolbox/event/# is the trigger surface."""
    assert mp.EVENT_RUN_STARTED_TOPIC == "image-toolbox/event/run_started"
    assert mp.EVENT_RUN_FINISHED_TOPIC == "image-toolbox/event/run_finished"
    for topic in (mp.LAST_RUN_TOPIC, mp.TASK_NAME_TOPIC, mp.AVAILABILITY_TOPIC):
        assert not topic.startswith("image-toolbox/event/")


# ── retain semantics ─────────────────────────────────────────────────────────

def test_a_retained_publish_is_remembered_for_the_reconnect_replay():
    c = _client()
    c.publish(mp.LAST_RUN_TOPIC, '{"processed": 3}')
    assert c._retained[mp.LAST_RUN_TOPIC] == '{"processed": 3}'


def test_an_event_publish_is_never_remembered():
    """The whole point: nothing may re-send it later. A remembered event would be
    replayed on the next reconnect and fire the automation a second time."""
    c = _client()
    c.publish(mp.EVENT_RUN_FINISHED_TOPIC, '{"processed": 3}', retain=False)
    assert mp.EVENT_RUN_FINISHED_TOPIC not in c._retained


def test_reconnect_replays_state_but_not_events():
    c = _client()
    c.publish(mp.TASK_NAME_TOPIC, "idle")
    c.publish(mp.LAST_RUN_TOPIC, '{"processed": 3}')
    c.publish(mp.EVENT_RUN_FINISHED_TOPIC, '{"processed": 3}', retain=False, qos=1)
    fake = _FakeClient()
    c._on_connect(fake, None, None, None)
    replayed = fake.topics()
    assert mp.TASK_NAME_TOPIC in replayed and mp.LAST_RUN_TOPIC in replayed
    assert mp.EVENT_RUN_FINISHED_TOPIC not in replayed
    # ...and the connect announcement itself is the availability topic, retained.
    assert (mp.AVAILABILITY_TOPIC, mp.ONLINE, 1, True) == fake.calls[0]


def test_every_replayed_value_goes_back_out_retained():
    c = _client()
    c.publish(mp.TASK_DETAILS_TOPIC, "working")
    fake = _FakeClient()
    c._on_connect(fake, None, None, None)
    for _topic, _payload, _qos, retain in fake.calls:
        assert retain is True


# ── publish_many passthrough ─────────────────────────────────────────────────

def test_publish_many_defaults_to_retained_state():
    c = _client()
    c.publish_many({mp.TASK_NAME_TOPIC: "upscaling", mp.TASK_ETA_TOPIC: "5m"})
    assert set(c._retained) == {mp.TASK_NAME_TOPIC, mp.TASK_ETA_TOPIC}


def test_publish_many_forwards_retain_and_qos(monkeypatch):
    c = _client()
    seen = []
    monkeypatch.setattr(c, "publish",
                        lambda topic, payload, retain=True, qos=0:
                        seen.append((topic, retain, qos)))
    c.publish_many({mp.EVENT_RUN_STARTED_TOPIC: "{}"}, retain=False, qos=1)
    assert seen == [(mp.EVENT_RUN_STARTED_TOPIC, False, 1)]
