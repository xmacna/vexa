# gmeet-capture/src

Front door [`index.ts`](index.ts). The browser pieces:
[`pcm-capture.ts`](pcm-capture.ts) (per-element `AudioContext` → 16 kHz PCM via the `WORKLET_SRC`
AudioWorklet — loaded from a host-supplied `moduleUrl` under MV3, else a `blob:` URL),
[`gmeet-capture.ts`](gmeet-capture.ts) (rescan + per-channel wiring),
[`gmeet-speakers.ts`](gmeet-speakers.ts) (the live glow), and [`gmeet-chat.ts`](gmeet-chat.ts)
(new-message capture plus send confirmation by DOM readback). The pure attribution logic:
[`gmeet-capture-v1.ts`](gmeet-capture-v1.ts) (the `capture.v1` producer + `pickBoundName`) and
[`gmeet-channel-binder.ts`](gmeet-channel-binder.ts) (energy↔glow correlation — DOM-free).

`gmeet-capture.test.ts` is the pure-core golden (`pickBoundName` + the energy↔glow channel binder);
`gmeet-speakers.test.ts` is the L2 unit for the glow→START/END hint edges, self-tile suppression, and
junk-name filtering (in-memory DOM shim). `gmeet-chat.test.ts` exercises sanitized current/legacy DOM
fixtures in jsdom; real Meet remains the L4 bar. All run on `npm test` (`gate:node`).
