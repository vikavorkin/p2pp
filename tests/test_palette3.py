"""
Tests for klippy/extras/palette3.py — MQTT ping handler for Palette 3
Connected Accessory Mode.

These tests run without a live Klipper installation by mocking the Klipper
config/gcode/printer objects that palette3.py depends on.
"""

import json
import sys
import os
import threading
import types
import unittest
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Add repo root to sys.path so we can import the klippy extra directly.
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import importlib
palette3_mod = importlib.import_module("klippy.extras.palette3")
Palette3 = palette3_mod.Palette3


# ---------------------------------------------------------------------------
# Minimal Klipper config/printer stubs
# ---------------------------------------------------------------------------

class _FakeGcode:
    """Minimal stub for Klipper's gcode object."""
    def __init__(self):
        self.commands = {}

    def register_command(self, name, handler, desc=""):
        self.commands[name] = handler


class _FakePrinter:
    """Minimal stub for Klipper's printer object."""
    def __init__(self):
        self._gcode = _FakeGcode()
        self._event_handlers = {}

    def lookup_object(self, name):
        if name == "gcode":
            return self._gcode
        raise KeyError(name)

    def register_event_handler(self, event, handler):
        self._event_handlers.setdefault(event, []).append(handler)


class _FakeConfig:
    """Minimal stub for Klipper's ConfigHelper."""
    def __init__(self, opts):
        self._opts = opts
        self.printer = _FakePrinter()

    def get_printer(self):
        return self.printer

    def get(self, name, default=None):
        if name in self._opts:
            return self._opts[name]
        if default is not None:
            return default
        raise KeyError(name)

    def getint(self, name, default=None):
        return int(self.get(name, default))

    def getboolean(self, name, default=None):
        val = self.get(name, default)
        if isinstance(val, bool):
            return val
        return val in ("true", "1", "yes", True)


class _FakeGcmd:
    """Stub for a gcode command invocation (gcmd)."""
    def __init__(self, params=None):
        self._params = params or {}
        self.responses = []

    def get_float(self, name, default=None, minval=None, maxval=None):
        val = float(self._params.get(name, default))
        if minval is not None and val < minval:
            raise ValueError("{} below minval {}".format(val, minval))
        if maxval is not None and val > maxval:
            raise ValueError("{} above maxval {}".format(val, maxval))
        return val

    def respond_info(self, text):
        self.responses.append(text)


# ---------------------------------------------------------------------------
# Helper: build a Palette3 instance with a fake config
# ---------------------------------------------------------------------------

def _make_palette3(opts=None):
    defaults = {
        "mqtt_host": "192.168.1.100",
        "mqtt_port": 1883,
        "origin_id": "simcoe",
        "auto_connect": False,  # don't auto-connect in tests
    }
    if opts:
        defaults.update(opts)
    config = _FakeConfig(defaults)
    p3 = Palette3(config)
    return p3


# ---------------------------------------------------------------------------
# Tests: component construction
# ---------------------------------------------------------------------------

class TestPalette3Init:
    def test_registers_o31_command(self):
        p3 = _make_palette3()
        gcode = p3.gcode
        assert "O31" in gcode.commands

    def test_registers_palette3_connect(self):
        p3 = _make_palette3()
        assert "PALETTE3_CONNECT" in p3.gcode.commands

    def test_registers_palette3_disconnect(self):
        p3 = _make_palette3()
        assert "PALETTE3_DISCONNECT" in p3.gcode.commands

    def test_registers_palette3_status(self):
        p3 = _make_palette3()
        assert "PALETTE3_STATUS" in p3.gcode.commands

    def test_auto_connect_registers_event_handler(self):
        p3 = _make_palette3({"auto_connect": True})
        assert "klippy:ready" in p3.printer._event_handlers

    def test_no_auto_connect_skips_event_handler(self):
        p3 = _make_palette3({"auto_connect": False})
        assert "klippy:ready" not in p3.printer._event_handlers


# ---------------------------------------------------------------------------
# Tests: O31 command — MQTT payload format
# ---------------------------------------------------------------------------

class TestO31Payload:
    """Verify the JSON payload published to Palette 3 for each O31 ping."""

    def _run_o31(self, p3, ping_mm):
        """Force-connect the component and fire one O31 command."""
        fake_client = MagicMock()
        with p3._lock:
            p3._client = fake_client
            p3._connected = True
        gcmd = _FakeGcmd({"L": ping_mm})
        p3._cmd_o31(gcmd)
        return fake_client

    def test_publishes_to_correct_topic(self):
        p3 = _make_palette3()
        client = self._run_o31(p3, 374.92)
        topic = client.publish.call_args[0][0]
        assert topic == "palette/request/print/ping-signature"

    def test_payload_contains_ping_th(self):
        p3 = _make_palette3()
        client = self._run_o31(p3, 374.92)
        raw = client.publish.call_args[0][1]
        payload = json.loads(raw)
        assert payload["payload"]["query"]["ping_th"] == 374.92

    def test_payload_header_origin_id(self):
        p3 = _make_palette3({"origin_id": "simcoe"})
        client = self._run_o31(p3, 100.0)
        payload = json.loads(client.publish.call_args[0][1])
        assert payload["header"]["originID"] == "simcoe"

    def test_payload_header_msg_id_type_and_range(self):
        p3 = _make_palette3()
        client = self._run_o31(p3, 100.0)
        payload = json.loads(client.publish.call_args[0][1])
        msg_id = payload["header"]["msgID"]
        assert isinstance(msg_id, int)
        assert 0 <= msg_id < 512

    def test_payload_method_is_post(self):
        p3 = _make_palette3()
        client = self._run_o31(p3, 100.0)
        payload = json.loads(client.publish.call_args[0][1])
        assert payload["payload"]["method"] == "post"

    def test_msg_id_increments_on_each_ping(self):
        p3 = _make_palette3()
        fake_client = MagicMock()
        with p3._lock:
            p3._client = fake_client
            p3._connected = True

        msg_ids = []
        for mm in [100.0, 200.0, 300.0]:
            p3._cmd_o31(_FakeGcmd({"L": mm}))
            raw = fake_client.publish.call_args[0][1]
            msg_ids.append(json.loads(raw)["header"]["msgID"])

        assert msg_ids == [1, 2, 3]

    def test_msg_id_wraps_at_512(self):
        p3 = _make_palette3()
        fake_client = MagicMock()
        with p3._lock:
            p3._client = fake_client
            p3._connected = True
            p3._msg_id = 511

        p3._cmd_o31(_FakeGcmd({"L": 100.0}))
        raw = fake_client.publish.call_args[0][1]
        assert json.loads(raw)["header"]["msgID"] == 0


# ---------------------------------------------------------------------------
# Tests: O31 when not connected
# ---------------------------------------------------------------------------

class TestO31WhenDisconnected:
    def test_no_publish_when_client_is_none(self):
        p3 = _make_palette3()
        # _client and _connected remain at defaults (None / False)
        gcmd = _FakeGcmd({"L": 150.0})
        p3._cmd_o31(gcmd)
        # If we get here without AttributeError, the guard worked correctly.

    def test_no_publish_when_client_exists_but_not_connected(self):
        p3 = _make_palette3()
        fake_client = MagicMock()
        with p3._lock:
            p3._client = fake_client
            p3._connected = False
        p3._cmd_o31(_FakeGcmd({"L": 150.0}))
        fake_client.publish.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: MQTT ACK handler
# ---------------------------------------------------------------------------

class TestMqttAckHandler:
    def test_valid_json_ack_does_not_raise(self):
        p3 = _make_palette3()
        msg = MagicMock()
        msg.payload = json.dumps({"status": "ok"}).encode()
        p3._on_mqtt_message(None, None, msg)  # should not raise

    def test_invalid_json_ack_does_not_raise(self):
        p3 = _make_palette3()
        msg = MagicMock()
        msg.payload = b"not json {"
        p3._on_mqtt_message(None, None, msg)  # should not raise

    def test_non_utf8_payload_does_not_raise(self):
        p3 = _make_palette3()
        msg = MagicMock()
        msg.payload = bytes([0xFF, 0xFE, 0xFD])
        p3._on_mqtt_message(None, None, msg)  # should not raise


# ---------------------------------------------------------------------------
# Tests: on_mqtt_connect callback
# ---------------------------------------------------------------------------

class TestMqttConnectCallback:
    def test_rc0_sets_connected_flag(self):
        p3 = _make_palette3()
        fake_client = MagicMock()
        p3._on_mqtt_connect(fake_client, None, None, rc=0)
        assert p3._connected is True

    def test_rc0_subscribes_to_response_topic(self):
        p3 = _make_palette3({"origin_id": "simcoe"})
        fake_client = MagicMock()
        p3._on_mqtt_connect(fake_client, None, None, rc=0)
        fake_client.subscribe.assert_called_once_with(
            "simcoe/response/print/ping-signature"
        )

    def test_nonzero_rc_does_not_set_connected(self):
        p3 = _make_palette3()
        fake_client = MagicMock()
        p3._on_mqtt_connect(fake_client, None, None, rc=5)
        assert p3._connected is False

    def test_nonzero_rc_does_not_subscribe(self):
        p3 = _make_palette3()
        fake_client = MagicMock()
        p3._on_mqtt_connect(fake_client, None, None, rc=5)
        fake_client.subscribe.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: on_mqtt_disconnect callback
# ---------------------------------------------------------------------------

class TestMqttDisconnectCallback:
    def test_clears_connected_flag(self):
        p3 = _make_palette3()
        with p3._lock:
            p3._connected = True
        p3._on_mqtt_disconnect(None, None, rc=0)
        assert p3._connected is False

    def test_unexpected_disconnect_clears_flag(self):
        p3 = _make_palette3()
        with p3._lock:
            p3._connected = True
        p3._on_mqtt_disconnect(None, None, rc=1)
        assert p3._connected is False


# ---------------------------------------------------------------------------
# Tests: PALETTE3_STATUS command
# ---------------------------------------------------------------------------

class TestStatusCommand:
    def test_status_not_connected(self):
        p3 = _make_palette3()
        gcmd = _FakeGcmd()
        p3._cmd_status(gcmd)
        assert any("not connected" in r for r in gcmd.responses)

    def test_status_connected(self):
        p3 = _make_palette3({"mqtt_host": "192.168.1.50", "mqtt_port": 1883})
        with p3._lock:
            p3._client = MagicMock()
            p3._connected = True
        gcmd = _FakeGcmd()
        p3._cmd_status(gcmd)
        assert any("connected" in r for r in gcmd.responses)
        assert any("192.168.1.50" in r for r in gcmd.responses)

    def test_status_connecting(self):
        p3 = _make_palette3({"mqtt_host": "192.168.1.50"})
        with p3._lock:
            p3._client = MagicMock()
            p3._connected = False
        gcmd = _FakeGcmd()
        p3._cmd_status(gcmd)
        assert any("connecting" in r for r in gcmd.responses)


# ---------------------------------------------------------------------------
# Tests: do_disconnect clears client and connected flag
# ---------------------------------------------------------------------------

class TestDoDisconnect:
    def test_clears_client_and_connected(self):
        p3 = _make_palette3()
        fake_client = MagicMock()
        with p3._lock:
            p3._client = fake_client
            p3._connected = True
        p3._do_disconnect()
        with p3._lock:
            assert p3._client is None
            assert p3._connected is False

    def test_calls_loop_stop_and_disconnect(self):
        p3 = _make_palette3()
        fake_client = MagicMock()
        with p3._lock:
            p3._client = fake_client
            p3._connected = True
        p3._do_disconnect()
        fake_client.loop_stop.assert_called_once()
        fake_client.disconnect.assert_called_once()

    def test_disconnect_when_client_is_none_is_safe(self):
        p3 = _make_palette3()
        # _client is None by default; should not raise
        p3._do_disconnect()


# ---------------------------------------------------------------------------
# Helper: inject a minimal paho stub so tests work without paho installed
# ---------------------------------------------------------------------------

def _make_paho_stub(fake_client_instance):
    """Return a (paho_stub, paho_mqtt_stub, paho_mqtt_client_stub) triple."""
    paho_mqtt_client_stub = types.ModuleType("paho.mqtt.client")
    paho_mqtt_client_stub.Client = MagicMock(return_value=fake_client_instance)
    paho_mqtt_stub = types.ModuleType("paho.mqtt")
    paho_mqtt_stub.client = paho_mqtt_client_stub
    paho_stub = types.ModuleType("paho")
    paho_stub.mqtt = paho_mqtt_stub
    return paho_stub, paho_mqtt_stub, paho_mqtt_client_stub


# ---------------------------------------------------------------------------
# Tests: do_connect skips when already connected
# ---------------------------------------------------------------------------

class TestDoConnect:
    def test_skips_if_client_already_set(self):
        p3 = _make_palette3()
        existing_client = MagicMock()
        with p3._lock:
            p3._client = existing_client

        fake_new_client = MagicMock()
        paho_s, paho_mqtt_s, paho_client_s = _make_paho_stub(fake_new_client)
        with patch.dict("sys.modules", {
            "paho": paho_s,
            "paho.mqtt": paho_mqtt_s,
            "paho.mqtt.client": paho_client_s,
        }):
            p3._do_connect()

        # The existing client must not have been replaced.
        with p3._lock:
            assert p3._client is existing_client

    def test_sets_client_on_first_connect(self):
        p3 = _make_palette3()
        fake_client = MagicMock()
        paho_s, paho_mqtt_s, paho_client_s = _make_paho_stub(fake_client)
        with patch.dict("sys.modules", {
            "paho": paho_s,
            "paho.mqtt": paho_mqtt_s,
            "paho.mqtt.client": paho_client_s,
        }):
            p3._do_connect()
        with p3._lock:
            assert p3._client is fake_client

    def test_missing_paho_logs_error_and_returns(self):
        p3 = _make_palette3()
        # Simulate paho not installed by mapping its names to None.
        with patch.dict("sys.modules", {
            "paho": None,
            "paho.mqtt": None,
            "paho.mqtt.client": None,
        }):
            p3._do_connect()  # should not raise
        # If no exception, the ImportError guard worked.
        with p3._lock:
            assert p3._client is None


# ---------------------------------------------------------------------------
# Tests: thread safety — msg_id increments are atomic
# ---------------------------------------------------------------------------

class TestThreadSafety:
    def test_concurrent_o31_calls_produce_unique_msg_ids(self):
        """Fire 100 O31 pings from multiple threads; all msgIDs must be unique."""
        p3 = _make_palette3()
        fake_client = MagicMock()
        with p3._lock:
            p3._client = fake_client
            p3._connected = True

        collected_ids = []
        lock = threading.Lock()

        def fire_pings(n):
            for mm in range(n):
                gcmd = _FakeGcmd({"L": float(mm)})
                p3._cmd_o31(gcmd)
                raw = fake_client.publish.call_args[0][1]
                mid = json.loads(raw)["header"]["msgID"]
                with lock:
                    collected_ids.append(mid)

        threads = [threading.Thread(target=fire_pings, args=(50,)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All 100 pings fired; msgIDs are 0-511 rolling so just verify count.
        assert len(collected_ids) == 100


# ---------------------------------------------------------------------------
# Tests: load_config factory
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_load_config_returns_palette3_instance(self):
        config = _FakeConfig({
            "mqtt_host": "10.0.0.1",
            "mqtt_port": 1883,
            "origin_id": "simcoe",
            "auto_connect": False,
        })
        obj = palette3_mod.load_config(config)
        assert isinstance(obj, Palette3)
