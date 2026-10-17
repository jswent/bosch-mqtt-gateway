# Home Assistant mapping

The gateway advertises MQTT entities through retained discovery. Enable MQTT discovery in Home Assistant using the same broker and discovery prefix. Retained discovery and state messages allow Home Assistant to restart without requiring the gateway to subscribe to HA birth messages. Discovery identifiers include the gateway ID, panel serial, entity kind and panel object ID; renaming a point does not replace its entity. UTF-8 hex encoding prevents collisions between names or punctuation in identifiers. MQTT identities are intentionally separate from the native integration; existing entity IDs and dashboard references are not automatically migrated. See [MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery).

| Native Bosch entity | Gateway entity | Preserved behavior |
| --- | --- | --- |
| Area alarm control panel | MQTT alarm control panel | Triggered/disarmed/arming/pending/armed_home/armed_away telemetry and native precedence |
| Area ready home/away | Diagnostic binary sensor | Home = all-ready or part-ready; away = all-ready |
| Area burglary/gas/fire issues | Enum sensor | no_issues, supervisory, trouble, alarm; native priority order |
| Area faulting points | Numeric sensor | Count, unit points |
| Point | Binary sensor | Native is_open result; null stays unknown |
| Output switch | Binary sensor | Native active boolean, no output controls |
| Door switches | Three binary sensors | Locked, Secured, Momentarily unlocked booleans |
| Panel faults | Diagnostic binary sensors | All 12 native masks, device classes and default-enabled choices |

Area, point, output and door devices retain panel-provided names and reference the Bosch panel parent with `via_device`. Faults belong to the parent device. Panels without fault support receive a diagnostic Panel connection sensor so the parent device can still be registered. Raw status, status text, alarm IDs and other adapter observations are exposed as JSON attributes; the native integration currently exposes fewer raw attributes. No history sensor is created: the native sensor module's historical docstring is stale. Names are English; MQTT discovery does not inherit Bosch integration translations. See [MQTT sensor](https://www.home-assistant.io/integrations/sensor.mqtt/) and [MQTT binary sensor](https://www.home-assistant.io/integrations/binary_sensor.mqtt/).

## Alarm controls are deliberately inert

Home Assistant's MQTT alarm platform **requires `command_topic`**. The user requested the native alarm entity UI, so discovery declares a reserved `{topic_prefix}/command/area/{encoded_id}` placeholder. `DiscoveryBuilder.build()` in `src/bosch_mqtt_gateway/discovery.py` documents this future extension point. The gateway never subscribes, receives, dispatches or executes MQTT commands; its publisher allowlist excludes command topics. The supplied broker ACL also denies publishing to the command namespace. Arming features are an empty list. Home Assistant may still display a Disarm control, but it cannot change the panel. No optimistic state is emitted: only actual panel telemetry changes the alarm state. Unknown telemetry resets the HA state instead of preserving a previous arm state. Future control support requires a separate security design, authorization, broker permissions and an explicit command implementation. See the required command topic and supported features in [MQTT alarm control panel](https://www.home-assistant.io/integrations/alarm_control_panel.mqtt/).

Door Locked deliberately has no `lock` device class: Home Assistant interprets ON for that class as *unlocked*, the opposite of the native Bosch Locked switch. Phone line failure retains the native connectivity class and polarity, even though that means ON denotes a failure. Most faults are disabled by default, matching native Bosch; Battery, Battery missing and AC failure are enabled. Initial readiness remains unknown until the panel reports it.

## Availability, persistence and cleanup

Telemetry entities require both gateway availability and panel availability to be online. The broker's retained last will revokes gateway availability after an unclean disconnect. The runtime marks offline before synchronization, publishes retained discovery and snapshots at bounded QoS 1, catches up queued changes, then marks online. Unknown is distinct from unavailable. Retained data can survive broker restarts; broker persistence must be enabled. Discovery cleanup publishes empty retained tombstones only for previously journaled topics in this gateway's validated namespace. Keep the durable topic journal when upgrading; changing gateway IDs or prefixes creates different identities and requires cleanup of the old deployment.

Research baseline: local Home Assistant `homeassistant/components/bosch_alarm/{entity,alarm_control_panel,binary_sensor,sensor,switch,strings}.py` and MQTT platform schemas, reviewed 2026-10-17. No Home Assistant repository files were changed.
