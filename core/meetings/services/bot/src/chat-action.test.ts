import { interactiveHandler } from './index.js';

let failed = 0;
const check = (name: string, condition: boolean) => {
  console.log(`  ${condition ? '✅' : '❌'} ${name}`);
  if (!condition) failed++;
};

const spoken: string[] = [];
const speak = {
  async speak(text: string) { spoken.push(text); },
  async stop() { spoken.push('STOP'); },
};
const sent: string[] = [];
const page = {
  async evaluate(fn: (text: string) => Promise<unknown>, text: string) {
    (globalThis as any).__vexaGmeetChat = {
      async send(value: string) { sent.push(value); return { confirmed: true }; },
    };
    try { return await fn(text); }
    finally { delete (globalThis as any).__vexaGmeetChat; }
  },
};

const handler = interactiveHandler(speak, page as any);
await handler({ action: 'chat_send', text: 'Resposta da empresa' });
await handler({ action: 'speak', text: 'Olá' });
check('chat_send reaches the page-side confirmed sender', sent[0] === 'Resposta da empresa');
check('voice acts still reach the speak controller', spoken[0] === 'Olá');

let rejected = false;
const unavailable = interactiveHandler(speak, {
  async evaluate() { return { confirmed: false, reason: 'composer_not_found' }; },
} as any);
try { await unavailable({ action: 'chat_send', text: 'x' }); }
catch { rejected = true; }
check('unconfirmed Meet sends fail loudly', rejected);

const order: string[] = [];
let releaseLegacy!: () => void;
const legacyGate = new Promise<void>((resolve) => { releaseLegacy = resolve; });
const shared = interactiveHandler(speak, {
  async evaluate() {
    order.push('legacy-start');
    await legacyGate;
    order.push('legacy-finish');
    return { confirmed: true };
  },
} as any, async () => { order.push('v2'); });
const legacyRun = shared({ action: 'chat_send', text: 'mesmo texto' });
const durableRun = shared({
  action: 'chat_send_v2',
  assignmentId: '11111111-1111-4111-8111-111111111111',
  commandId: '33333333-3333-4333-8333-333333333333',
  meetingId: 7,
  payloadHash: 'a'.repeat(64),
  text: 'mesmo texto',
});
await Promise.resolve();
check('legacy and v2 share one browser chat mutex', order.join(',') === 'legacy-start');
releaseLegacy();
await Promise.all([legacyRun, durableRun]);
check('v2 enters only after legacy DOM readback finishes',
  order.join(',') === 'legacy-start,legacy-finish,v2');

let legacyAmbiguousDom = 0;
let legacyQuarantines = 0;
let durableAfterAmbiguous = 0;
const fenced = interactiveHandler(
  speak,
  {
    async evaluate() {
      legacyAmbiguousDom++;
      return { confirmed: false, reason: 'message_not_observed_after_send' };
    },
  } as any,
  async () => { durableAfterAmbiguous++; },
  async () => { legacyQuarantines++; },
);
await fenced({ action: 'chat_send', text: 'mesmo texto' }).catch(() => undefined);
await fenced({
  action: 'chat_send_v2',
  assignmentId: '11111111-1111-4111-8111-111111111111',
  commandId: '44444444-4444-4444-8444-444444444444',
  meetingId: 7,
  payloadHash: 'b'.repeat(64),
  text: 'mesmo texto',
}).catch(() => undefined);
check('legacy ambiguity quarantines the browser and fences a later equal-text v2 command',
  legacyAmbiguousDom === 1 && legacyQuarantines === 1 && durableAfterAmbiguous === 0);

let sharedQuarantined = false;
let legacyAfterDurableUnknown = 0;
const reverseFenced = interactiveHandler(
  speak,
  { async evaluate() { legacyAfterDurableUnknown++; return { confirmed: true }; } } as any,
  async () => { sharedQuarantined = true; },
  async () => { sharedQuarantined = true; },
  () => sharedQuarantined,
);
await reverseFenced({
  action: 'chat_send_v2', assignmentId: '11111111-1111-4111-8111-111111111111',
  commandId: '55555555-5555-4555-8555-555555555555', meetingId: 7,
  payloadHash: 'c'.repeat(64), text: 'mesmo texto',
});
await reverseFenced({ action: 'chat_send', text: 'mesmo texto' }).catch(() => undefined);
check('v2 quarantine state symmetrically fences a queued legacy DOM attempt',
  sharedQuarantined && legacyAfterDurableUnknown === 0);

let hangingLegacyCalls = 0;
let afterHangingV2 = 0;
const timeoutFenced = interactiveHandler(
  speak,
  { async evaluate() { hangingLegacyCalls++; return new Promise(() => {}); } } as any,
  async () => { afterHangingV2++; },
  async () => {},
  undefined,
  10,
);
const hung = timeoutFenced({ action: 'chat_send', text: 'x' }).catch(() => undefined);
const afterHung = timeoutFenced({
  action: 'chat_send_v2', assignmentId: '11111111-1111-4111-8111-111111111111',
  commandId: '66666666-6666-4666-8666-666666666666', meetingId: 7,
  payloadHash: 'd'.repeat(64), text: 'x',
}).catch(() => undefined);
await Promise.all([hung, afterHung]);
check('hanging legacy DOM send is bounded and fences the queued v2 command',
  hangingLegacyCalls === 1 && afterHangingV2 === 0);

if (failed) process.exit(1);
console.log('\n✅ chat-action: acts.v1 chat_send is confirmed by the browser adapter.');
