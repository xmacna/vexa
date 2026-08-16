# acts.v1 — the bot command bus

Control-plane → bot, over redis pub/sub on **`bot_commands:meeting:{meeting_id}`**. One JSON message per
command, discriminated by `action`; **unknown actions are ignored** (forward-compatible).

## Commands
- **Core control** (always honored): `leave` · `reconfigure` (language/task/allowedLanguages).
- **Voice agent** (optional, gated by `voiceAgentEnabled` in the invocation): `speak` · `speak_audio` ·
  `speak_stop` · `chat_send` · `chat_read` · `screen_show` · `screen_stop` · `avatar_set` · `avatar_reset`.
- **Confirmed chat:** `chat_send_v2` carries immutable assignment, command and payload bindings. A
  new bot claims it durably before touching the DOM and reports the correlated readback result.
  Old bots ignore this unknown action; the command remains pending until its server-side expiry.

`Act` is the `oneOf` of all variants (`$defs`). No auth token (transport-layer), no tenancy fields.
Goldens (`Act.<case>.json`) validate against `#/$defs/Act` via `gate:schema`.
