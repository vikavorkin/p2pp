"""
klippy/extras/palette3.py — Palette 3 Connected Accessory Mode ping handler

Intercepts O31 L<mm> mm gcode commands emitted by P2PP in Connected Mode
and forwards each ping to the Palette 3 device via MQTT.

P2PP generates O31 L<mm> mm for Palette 3 Connected Mode (PALETTE3 /
PALETTE3_PRO keywords).  When Klipper — rather than OctoPrint — is printing
the gcode, this component replaces the role of OctoPrint's serial forwarding
and sends each ping directly to the Palette 3 over MQTT.

Palette 3 MQTT API
------------------
Publish topic:   palette/request/print/ping-signature
Subscribe topic: {origin_id}/response/print/ping-signature
Payload format:
  {
    "header": {"originID": "<origin_id>", "msgID": <0-511>},
    "payload": {"method": "post", "query": {"ping_th": <mm>}}
  }

where ``ping_th`` is the extrusion distance in mm taken from the O31 command,
and ``msgID`` is a rolling counter (0–511) used for request/response matching.

printer.cfg example
-------------------
  [palette3]
  mqtt_host: 192.168.1.100
  # mqtt_port: 1883          (optional, default 1883)
  # origin_id: simcoe        (optional, default "simcoe")
  # auto_connect: true       (optional, default true — connect on Klipper ready)

Gcode commands
--------------
  O31 L<mm>           — Palette 3 ping (injected by P2PP into Connected Mode gcode)
  PALETTE3_CONNECT    — Connect to Palette 3 MQTT broker
  PALETTE3_DISCONNECT — Disconnect from Palette 3 MQTT broker
  PALETTE3_STATUS     — Report current connection state

Installation
------------
  Symlink or copy this file into Klipper's extras directory:
      ln -s ~/p2pp/klippy/extras/palette3.py ~/klipper/klippy/extras/palette3.py
  Install paho-mqtt into the Klipper virtualenv:
      ~/klippy-env/bin/pip install paho-mqtt
  Restart Klipper.
"""

import json
import logging
import threading

logger = logging.getLogger(__name__)

# msgID wraps around after reaching this value (0–511 per Palette 3 protocol).
_MSG_ID_MAX = 512

# MQTT topic Palette 3 listens on for ping requests.
_PING_REQUEST_TOPIC = "palette/request/print/ping-signature"


class Palette3:
    """Klipper component for Palette 3 Connected Accessory Mode."""

    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")

        self._host = config.get("mqtt_host")
        self._port = config.getint("mqtt_port", 1883)
        self._origin_id = config.get("origin_id", "simcoe")
        self._auto_connect = config.getboolean("auto_connect", True)

        # State guarded by _lock (accessed from both reactor and paho threads).
        self._lock = threading.Lock()
        self._client = None      # paho Client instance, or None
        self._connected = False  # True only after on_connect with rc==0
        self._msg_id = 0         # rolling 0-511 counter

        self.gcode.register_command(
            "O31",
            self._cmd_o31,
            desc="Palette 3 ping (Connected Mode)",
        )
        self.gcode.register_command(
            "PALETTE3_CONNECT",
            self._cmd_connect,
            desc="Connect to the Palette 3 MQTT broker",
        )
        self.gcode.register_command(
            "PALETTE3_DISCONNECT",
            self._cmd_disconnect,
            desc="Disconnect from the Palette 3 MQTT broker",
        )
        self.gcode.register_command(
            "PALETTE3_STATUS",
            self._cmd_status,
            desc="Report Palette 3 MQTT connection state",
        )

        if self._auto_connect:
            self.printer.register_event_handler(
                "klippy:ready", self._on_klippy_ready
            )

    # ------------------------------------------------------------------
    # Klipper lifecycle
    # ------------------------------------------------------------------

    def _on_klippy_ready(self):
        self._do_connect()

    # ------------------------------------------------------------------
    # MQTT connection management (may be called from reactor or gcode thread)
    # ------------------------------------------------------------------

    def _do_connect(self):
        """Start the MQTT client and connect asynchronously."""
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            logger.error(
                "palette3: paho-mqtt is not installed — "
                "run: ~/klippy-env/bin/pip install paho-mqtt"
            )
            return

        with self._lock:
            if self._client is not None:
                logger.debug("palette3: already connected or connecting — ignoring")
                return
            client = mqtt.Client(client_id=self._origin_id)
            self._client = client

        client.on_connect = self._on_mqtt_connect
        client.on_disconnect = self._on_mqtt_disconnect
        client.on_message = self._on_mqtt_message

        try:
            client.connect_async(self._host, self._port)
            client.loop_start()
            logger.info(
                "palette3: connecting to MQTT broker at %s:%d",
                self._host,
                self._port,
            )
        except Exception as exc:
            logger.error("palette3: MQTT connect_async failed: %s", exc)
            with self._lock:
                self._client = None

    def _do_disconnect(self):
        """Stop the MQTT client and mark as disconnected."""
        with self._lock:
            client = self._client
            self._client = None
            self._connected = False

        if client is not None:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception as exc:
                logger.warning("palette3: error during disconnect: %s", exc)

    # ------------------------------------------------------------------
    # paho-mqtt callbacks (run in paho's background thread, not reactor)
    # ------------------------------------------------------------------

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        if rc != 0:
            logger.error(
                "palette3: MQTT broker refused connection (rc=%d)", rc
            )
            return

        resp_topic = "{}/response/print/ping-signature".format(self._origin_id)
        client.subscribe(resp_topic)
        with self._lock:
            self._connected = True
        logger.info(
            "palette3: MQTT connected; subscribed to %s", resp_topic
        )

    def _on_mqtt_disconnect(self, client, userdata, rc):
        with self._lock:
            self._connected = False
        if rc == 0:
            logger.info("palette3: MQTT disconnected cleanly")
        else:
            logger.warning(
                "palette3: MQTT disconnected unexpectedly (rc=%d)", rc
            )

    def _on_mqtt_message(self, client, userdata, msg):
        """Handle ping ACK from Palette 3."""
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            logger.warning("palette3: could not decode ACK payload: %s", exc)
            return
        logger.info("palette3: ping ACK received: %s", payload)

    # ------------------------------------------------------------------
    # Gcode command handlers (run in Klipper's reactor thread)
    # ------------------------------------------------------------------

    def _cmd_o31(self, gcmd):
        """O31 L<mm> — Palette 3 ping (Connected Mode).

        P2PP embeds these commands in the gcode at each ping position.
        The L parameter carries the cumulative extrusion in mm at that point.
        """
        ping_mm = gcmd.get_float("L", minval=0.0)

        with self._lock:
            connected = self._connected
            client = self._client
            self._msg_id = (self._msg_id + 1) % _MSG_ID_MAX
            msg_id = self._msg_id

        if not connected or client is None:
            logger.warning(
                "palette3: O31 ping skipped — not connected to Palette 3 "
                "(ping_th=%.2f mm)",
                ping_mm,
            )
            return

        payload = {
            "header": {"originID": self._origin_id, "msgID": msg_id},
            "payload": {"method": "post", "query": {"ping_th": ping_mm}},
        }
        client.publish(_PING_REQUEST_TOPIC, json.dumps(payload))
        logger.info(
            "palette3: ping sent (msgID=%d, ping_th=%.2f mm)", msg_id, ping_mm
        )

    def _cmd_connect(self, gcmd):
        """PALETTE3_CONNECT — connect to MQTT broker."""
        self._do_connect()
        gcmd.respond_info(
            "Palette3: connecting to {}:{}".format(self._host, self._port)
        )

    def _cmd_disconnect(self, gcmd):
        """PALETTE3_DISCONNECT — disconnect from MQTT broker."""
        self._do_disconnect()
        gcmd.respond_info("Palette3: disconnected")

    def _cmd_status(self, gcmd):
        """PALETTE3_STATUS — report connection state."""
        with self._lock:
            connected = self._connected
            client = self._client

        if client is None:
            state = "not connected"
        elif connected:
            state = "connected to {}:{}".format(self._host, self._port)
        else:
            state = "connecting to {}:{}".format(self._host, self._port)
        gcmd.respond_info("Palette3: {}".format(state))


def load_config(config):
    return Palette3(config)
