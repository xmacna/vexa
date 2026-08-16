import {
  createConfirmedChatHandler,
  createHttpChatCommandApi,
  probeConfirmedChatBridge,
  quarantineChatSession,
  type ChatCommandApi,
} from './chat-command.js';
import type { Act } from './contracts.js';
import { createOrchestrator } from './orchestrator.js';

let failed = 0;
const check = (name: string, condition: boolean) => {
  console.log(`  ${condition ? '✅' : '❌'} ${name}`);
  if (!condition) failed++;
};

const act: Extract<Act, { action: 'chat_send_v2' }> = {
  action: 'chat_send_v2',
  assignmentId: '11111111-1111-4111-8111-111111111111',
  commandId: '33333333-3333-4333-8333-333333333333',
  meetingId: 7,
  payloadHash: 'a'.repeat(64),
  text: 'resposta',
};

const apiEvents: string[] = [];
const api: ChatCommandApi = {
  async claim(command) {
    apiEvents.push(`claim:${command.commandId}`);
    return { status: 'claimed', claimToken: 'opaque-claim', command };
  },
  async result(_command, result) {
    apiEvents.push(`result:${result.status}:${result.reason ?? ''}`);
  },
  async pending() { return [act]; },
};
const dom: string[] = [];
const page = {
  async evaluate(fn: (text: string) => Promise<unknown>, text: string) {
    (globalThis as any).__vexaGmeetChat = {
      async send(value: string) { dom.push(value); return { confirmed: true }; },
    };
    try { return await fn(text); }
    finally { delete (globalThis as any).__vexaGmeetChat; }
  },
};
const handler = createConfirmedChatHandler({
  api,
  page: page as any,
  meetingId: 7,
  assignmentId: act.assignmentId,
  quarantine: async () => {},
});

await Promise.all([handler(act), handler(act), handler(act)]);
check('concurrent duplicate deliveries produce one DOM attempt', dom.length === 1);
check('claim is durable before DOM result callback', apiEvents.join(',') ===
  `claim:${act.commandId},result:confirmed:`);

const ambiguousEvents: string[] = [];
let ambiguousQuarantines = 0;
let ambiguousDomCalls = 0;
const ambiguous = createConfirmedChatHandler({
  api: {
    ...api,
    async claim(command) { return { status: 'claimed', claimToken: 'opaque-claim-2', command }; },
    async result(_command, result) { ambiguousEvents.push(`${result.status}:${result.reason}`); },
  },
  page: { async evaluate() { ambiguousDomCalls++; return { confirmed: false, reason: 'message_not_observed_after_send' }; } } as any,
  meetingId: 7,
  assignmentId: act.assignmentId,
  quarantine: async () => { ambiguousQuarantines++; },
});
await ambiguous({ ...act, commandId: '44444444-4444-4444-8444-444444444444' });
await ambiguous({ ...act, commandId: '45454545-4545-4545-8545-454545454545' });
check('post-click ambiguity is terminal unknown, not success',
  ambiguousEvents[0] === 'indeterminate:message_not_observed_after_send');
check('post-click ambiguity quarantines the browser before another same-text DOM attempt',
  ambiguousQuarantines === 1 && ambiguousDomCalls === 1 && ambiguousEvents.length === 1);

const rejectedDom: string[] = [];
const rejected = createConfirmedChatHandler({
  api: {
    ...api,
    async claim() { return { status: 'rejected' }; },
  },
  page: { async evaluate(_fn: unknown, text: string) { rejectedDom.push(text); } } as any,
  meetingId: 7,
  assignmentId: act.assignmentId,
  quarantine: async () => {},
});
await rejected({ ...act, commandId: '55555555-5555-4555-8555-555555555555' });
check('claim rejection is fail-closed before DOM', rejectedDom.length === 0);

let releaseReady!: (ready: boolean) => void;
const readyGate = new Promise<boolean>((resolve) => { releaseReady = resolve; });
let readyClaims = 0;
const readinessHandler = createConfirmedChatHandler({
  api: {
    ...api,
    async claim(command) {
      readyClaims++;
      return { status: 'claimed', claimToken: 'opaque-ready', command };
    },
    async result() {},
  },
  page: page as any,
  meetingId: 7,
  assignmentId: act.assignmentId,
  ready: () => readyGate,
  quarantine: async () => {},
});
const beforeCapture = readinessHandler({
  ...act, commandId: '56565656-5656-4656-8656-565656565656',
});
await Promise.resolve();
check('active-before-capture gap cannot claim a command', readyClaims === 0);
releaseReady(true);
await beforeCapture;
check('pending command converges after the page chat bridge is ready', readyClaims === 1);
check('missing page chat adapter fails readiness closed', await probeConfirmedChatBridge({
  async evaluate() { return false; },
} as any) === false);
check('page probe rejection fails readiness closed', await probeConfirmedChatBridge({
  async evaluate() { throw new Error('context destroyed'); },
} as any) === false);

const canonicalDom: string[] = [];
const canonical = createConfirmedChatHandler({
  api: {
    ...api,
    async claim(command) {
      return { status: 'claimed', claimToken: 'opaque-claim-3', command: { ...command, text: 'ledger text' } };
    },
    async result() {},
  },
  page: {
    async evaluate(fn: (text: string) => Promise<unknown>, text: string) {
      (globalThis as any).__vexaGmeetChat = {
        async send(value: string) { canonicalDom.push(value); return { confirmed: true }; },
      };
      try { return await fn(text); }
      finally { delete (globalThis as any).__vexaGmeetChat; }
    },
  } as any,
  meetingId: 7,
  assignmentId: act.assignmentId,
  quarantine: async () => {},
});
await canonical({ ...act, commandId: '66666666-6666-4666-8666-666666666666', text: 'tampered redis text' });
check('DOM executes canonical ledger text, never Redis text', canonicalDom[0] === 'ledger text');

let hashMismatchRejected = false;
try {
  const hashBound = createConfirmedChatHandler({
    api: {
      ...api,
      async claim(command) {
        return {
          status: 'claimed', claimToken: 'opaque-hash',
          command: { ...command, payloadHash: 'a'.repeat(64) },
        };
      },
    },
    page: page as any,
    meetingId: 7,
    assignmentId: act.assignmentId,
    quarantine: async () => {},
  });
  await hashBound({
    ...act,
    commandId: '77777777-7777-4777-8777-777777777777',
    payloadHash: 'b'.repeat(64),
  });
} catch { hashMismatchRejected = true; }
check('claim response must preserve the Redis payload hash binding', hashMismatchRejected);

let bodyAbortObserved = false;
const hangingBodyApi = createHttpChatCommandApi({
  callbackUrl: 'https://meeting-api.example/bots/internal/callback/lifecycle',
  meetingToken: 'secret-not-logged',
  meetingId: 7,
  assignmentId: act.assignmentId,
  timeoutMs: 10,
  fetchImpl: async (_url, init) => {
    init?.signal?.addEventListener('abort', () => { bodyAbortObserved = true; });
    return {
      status: 200,
      ok: true,
      json: () => new Promise(() => {}),
    } as Response;
  },
});
let bodyTimedOut = false;
try { await hangingBodyApi.pending(); }
catch { bodyTimedOut = true; }
check('deadline covers response body parsing and aborts fetch', bodyTimedOut && bodyAbortObserved);

let quarantines = 0;
const timeoutResults: string[] = [];
const timeoutHandler = createConfirmedChatHandler({
  api: {
    ...api,
    async claim(command) { return { status: 'claimed', claimToken: 'opaque-timeout', command }; },
    async result(_command, result) { timeoutResults.push(result.status); },
  },
  page: { async evaluate() { return new Promise(() => {}); } } as any,
  meetingId: 7,
  assignmentId: act.assignmentId,
  quarantine: async () => { quarantines++; },
  domTimeoutMs: 10,
});
await timeoutHandler({ ...act, commandId: '99999999-9999-4999-8999-999999999999' });
await timeoutHandler({ ...act, commandId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa' });
check('stuck CDP quarantines the browser before returning indeterminate',
  quarantines === 1 && timeoutResults.join(',') === 'indeterminate');

const quarantineOrder: string[] = [];
await quarantineChatSession({
  stop: () => quarantineOrder.push('evicted'),
  close: async () => { quarantineOrder.push('close'); await new Promise(() => {}); },
  closeTimeoutMs: 10,
});
check('quarantine signals evicted before a stuck browser close and remains bounded',
  quarantineOrder.join(',') === 'evicted,close');

const lifecycle: Array<{ status: string; completion_reason?: string }> = [];
const orchestrator = createOrchestrator({
  platform: 'google_meet', meetingUrl: 'https://meet.google.com/abc-defg-hij', botName: 'B',
  redisUrl: 'redis://localhost:6379', connectionId: act.assignmentId,
  nativeMeetingId: 'abc-defg-hij',
} as any, {
  lifecycle: { async emit(event) { lifecycle.push(event); } },
  join: {
    async join(report) { await report('active'); return 'admitted' as const; },
    onRemoval() { return () => {}; }, async leave() {}, async withdraw() {},
  },
  pipeline: { async start() {}, async stop() {} },
  acts: { subscribe() { return () => {}; } },
  aloneness: { onAlone() { return () => {}; } },
});
const lifecycleHandler = createConfirmedChatHandler({
  api: {
    ...api,
    async claim(command) { return { status: 'claimed', claimToken: 'opaque-lifecycle', command }; },
    async result() {},
  },
  page: { async evaluate() { return new Promise(() => {}); } } as any,
  meetingId: 7,
  assignmentId: act.assignmentId,
  quarantine: () => quarantineChatSession({
    stop: () => orchestrator.stop('evicted'),
    close: async () => new Promise(() => {}),
    closeTimeoutMs: 10,
  }),
  domTimeoutMs: 10,
});
const worker = orchestrator.run();
while (!lifecycle.some((event) => event.status === 'active')) {
  await new Promise((resolve) => setTimeout(resolve, 0));
}
await lifecycleHandler({ ...act, commandId: 'abababab-abab-4bab-8bab-abababababab' });
const workerResult = await worker;
check('DOM indeterminate terminates the worker as lifecycle completed(evicted)',
  workerResult.exitCode === 0
  && workerResult.status === 'completed'
  && lifecycle.at(-1)?.status === 'completed'
  && lifecycle.at(-1)?.completion_reason === 'evicted');

if (failed) process.exit(1);
console.log('\n✅ confirmed-chat: durable claim fences a single correlated DOM attempt.');
