import { randomUUID } from 'node:crypto';
import { parseAct, type Act } from './contracts.js';
import type { BrowserSession } from './capture-bridge.js';

export type ConfirmedChatAct = Extract<Act, { action: 'chat_send_v2' }>;
export type ChatResult =
  | { status: 'confirmed'; reason?: never }
  | { status: 'failed'; reason: 'gmeet_chat_unavailable' | 'chat_destroyed' | 'composer_not_found' | 'empty_message' }
  | { status: 'indeterminate'; reason: 'message_not_observed_after_send' };

export interface ChatCommandApi {
  claim(command: ConfirmedChatAct): Promise<
    { status: 'claimed'; claimToken: string; command: ConfirmedChatAct } | { status: 'rejected' }
  >;
  result(command: ConfirmedChatAct, result: ChatResult & { claimToken: string }): Promise<void>;
  pending(): Promise<ConfirmedChatAct[]>;
}

export async function quarantineChatSession(options: {
  stop: () => void;
  close: () => Promise<void>;
  closeTimeoutMs?: number;
}): Promise<void> {
  // Fence the worker before touching CDP. A wedged browser close must not keep the
  // orchestrator active after the DOM result became indeterminate.
  options.stop();
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([
      options.close().catch(() => undefined),
      new Promise<void>((resolve) => {
        timer = setTimeout(resolve, options.closeTimeoutMs ?? 1_000);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/**
 * Control-plane side of the browser timing contract. Eight seconds covers the browser adapter's
 * default 3 s second-opener attempt, its 500 ms history quiet window, at least one 750 ms poll tick,
 * and a slow-machine margin. A single CDP sample remains bounded independently.
 */
export const CONFIRMED_CHAT_BRIDGE_PROBE_DEFAULTS = Object.freeze({
  timeoutMs: 8_000,
  pollMs: 100,
  sampleTimeoutMs: 1_500,
});

export async function probeConfirmedChatBridge(
  page: Pick<BrowserSession['page'], 'evaluate'>,
  options: { timeoutMs?: number; pollMs?: number; sampleTimeoutMs?: number } = {},
): Promise<boolean> {
  const timeoutMs = Math.max(0, options.timeoutMs ?? CONFIRMED_CHAT_BRIDGE_PROBE_DEFAULTS.timeoutMs);
  const pollMs = Math.max(1, options.pollMs ?? CONFIRMED_CHAT_BRIDGE_PROBE_DEFAULTS.pollMs);
  const deadline = Date.now() + timeoutMs;
  do {
    const remaining = Math.max(0, deadline - Date.now());
    const sampleBudget = timeoutMs === 0
      ? Math.max(1, options.sampleTimeoutMs ?? CONFIRMED_CHAT_BRIDGE_PROBE_DEFAULTS.sampleTimeoutMs)
      : Math.min(
        options.sampleTimeoutMs ?? CONFIRMED_CHAT_BRIDGE_PROBE_DEFAULTS.sampleTimeoutMs,
        Math.max(1, remaining),
      );
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      const sample = await Promise.race([
        page.evaluate(() => {
          const chat = ((globalThis as any).__vexaGmeetChat);
          if (typeof chat?.send !== 'function' || typeof chat?.getState !== 'function') return false;
          const state = chat.getState();
          return state?.panelFound === true && state?.primed === true && state?.composerFound === true;
        }).then((ready) => ({ kind: 'result' as const, ready })),
        new Promise<{ kind: 'timeout'; ready: false }>((resolve) => {
          timer = setTimeout(resolve, sampleBudget, { kind: 'timeout', ready: false });
        }),
      ]);
      if (sample.ready === true) return true;
      if (sample.kind === 'timeout') return false;
    } catch {
      return false;
    } finally {
      if (timer) clearTimeout(timer);
    }
    if (Date.now() >= deadline) return false;
    await new Promise((resolve) => setTimeout(resolve, Math.min(pollMs, deadline - Date.now())));
  } while (Date.now() <= deadline);
  return false;
}

async function bridgeReadyBeforeClaim(
  page: Pick<BrowserSession['page'], 'evaluate'>,
): Promise<boolean> {
  return probeConfirmedChatBridge(page, {
    timeoutMs: CONFIRMED_CHAT_BRIDGE_PROBE_DEFAULTS.sampleTimeoutMs,
    pollMs: CONFIRMED_CHAT_BRIDGE_PROBE_DEFAULTS.pollMs,
    sampleTimeoutMs: CONFIRMED_CHAT_BRIDGE_PROBE_DEFAULTS.sampleTimeoutMs,
  });
}

type FetchLike = (input: string | URL, init?: RequestInit) => Promise<Response>;

async function boundedRequest(
  fetchImpl: FetchLike,
  url: URL,
  init: RequestInit,
  timeoutMs: number,
  parseBody: boolean,
): Promise<{ status: number; ok: boolean; body?: unknown }> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      fetchImpl(url, { ...init, signal: controller.signal }).then(async (response) => ({
        status: response.status,
        ok: response.ok,
        body: parseBody && response.ok ? await response.json() : undefined,
      })),
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => {
          controller.abort();
          reject(new Error('chat command request timed out'));
        }, timeoutMs);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

export function createHttpChatCommandApi(options: {
  callbackUrl: string;
  meetingToken: string;
  meetingId: number;
  assignmentId: string;
  fetchImpl?: FetchLike;
  timeoutMs?: number;
}): ChatCommandApi {
  const fetchImpl = options.fetchImpl ?? fetch;
  const timeoutMs = options.timeoutMs ?? 3_000;
  const origin = new URL(options.callbackUrl);
  const claimantId = randomUUID();
  const headers = {
    authorization: `Bearer ${options.meetingToken}`,
    'content-type': 'application/json',
  };

  async function post(
    command: ConfirmedChatAct,
    suffix: 'claim' | 'result',
    body: object,
    parseBody: boolean,
  ): Promise<{ status: number; ok: boolean; body?: unknown }> {
    const url = new URL(`/internal/chat-commands/${command.commandId}/${suffix}`, origin);
    let last: unknown;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const response = await boundedRequest(fetchImpl, url, {
          method: 'POST', headers, body: JSON.stringify(body),
        }, timeoutMs, parseBody);
        if (response.status < 500) return response;
        last = new Error(`chat command callback unavailable (${response.status})`);
      } catch (error) {
        last = error;
      }
    }
    throw new Error(`chat command callback unavailable: ${last instanceof Error ? last.name : 'network_error'}`);
  }

  const binding = (command: ConfirmedChatAct) => ({
    meetingId: command.meetingId,
    assignmentId: command.assignmentId,
    payloadHash: command.payloadHash,
  });

  return {
    async claim(command) {
      const response = await post(
        command,
        'claim',
        { ...binding(command), claimantId },
        true,
      );
      if (response.status === 404 || response.status === 409) return { status: 'rejected' };
      if (!response.ok) throw new Error(`chat command claim rejected (${response.status})`);
      const body = response.body as Record<string, unknown> | undefined;
      const canonical = parseAct(body?.command);
      if (
        body?.status !== 'claimed'
        || typeof body.claimToken !== 'string'
        || canonical?.action !== 'chat_send_v2'
      ) {
        throw new Error('chat command claim response is invalid');
      }
      return { status: 'claimed', claimToken: body.claimToken, command: canonical };
    },
    async result(command, result) {
      const response = await post(command, 'result', { ...binding(command), ...result }, false);
      if (!response.ok) throw new Error(`chat command result rejected (${response.status})`);
    },
    async pending() {
      const url = new URL('/internal/chat-commands/pending', origin);
      url.searchParams.set('meetingId', String(options.meetingId));
      url.searchParams.set('assignmentId', options.assignmentId);
      const response = await boundedRequest(fetchImpl, url, { headers }, timeoutMs, true);
      if (!response.ok) throw new Error(`chat command pending drain rejected (${response.status})`);
      const body = response.body as { commands?: unknown[] };
      if (!Array.isArray(body.commands)) throw new Error('chat command pending response is invalid');
      return body.commands.flatMap((item) => {
        const act = parseAct(item);
        return act?.action === 'chat_send_v2' ? [act] : [];
      });
    },
  };
}

function classifyDomResult(value: unknown): ChatResult {
  const result = value as { confirmed?: unknown; reason?: unknown } | null;
  if (result?.confirmed === true) return { status: 'confirmed' };
  const reason = result?.reason;
  if (
    reason === 'gmeet_chat_unavailable'
    || reason === 'chat_destroyed'
    || reason === 'composer_not_found'
    || reason === 'empty_message'
  ) return { status: 'failed', reason };
  return { status: 'indeterminate', reason: 'message_not_observed_after_send' };
}

export function createConfirmedChatHandler(options: {
  api: ChatCommandApi;
  page: Pick<BrowserSession['page'], 'evaluate'>;
  meetingId: number;
  assignmentId: string;
  ready?: () => Promise<boolean>;
  quarantine: () => Promise<void>;
  domTimeoutMs?: number;
}): (act: ConfirmedChatAct) => Promise<void> {
  let tail = Promise.resolve();
  const inFlight = new Map<string, Promise<void>>();
  const attempted = new Set<string>();
  let quarantined = false;

  const execute = async (act: ConfirmedChatAct): Promise<void> => {
    if (quarantined || act.meetingId !== options.meetingId || act.assignmentId !== options.assignmentId) return;
    if (options.ready && !(await options.ready())) return;
    if (!(await bridgeReadyBeforeClaim(options.page))) return;
    const claim = await options.api.claim(act);
    if (claim.status !== 'claimed') return;
    const canonical = claim.command;
    if (
      canonical.commandId !== act.commandId
      || canonical.meetingId !== options.meetingId
      || canonical.assignmentId !== options.assignmentId
      || canonical.payloadHash !== act.payloadHash
    ) throw new Error('claimed chat command binding mismatch');
    attempted.add(canonical.commandId);
    let domResult: unknown;
    try {
      const evaluation = options.page.evaluate(async (text: string) => {
        const chat = ((globalThis as any).__vexaGmeetChat);
        if (!chat?.send) return { confirmed: false, reason: 'gmeet_chat_unavailable' };
        return chat.send(text);
      }, canonical.text);
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        domResult = await Promise.race([
          evaluation,
          new Promise<never>((_resolve, reject) => {
            timer = setTimeout(
              () => reject(new Error('DOM confirmation timed out')),
              options.domTimeoutMs ?? 7_000,
            );
          }),
        ]);
      } finally {
        if (timer) clearTimeout(timer);
      }
    } catch {
      domResult = { confirmed: false, reason: 'message_not_observed_after_send' };
    }
    const result = classifyDomResult(domResult);
    if (result.status === 'indeterminate' && !quarantined) {
      quarantined = true;
      await options.quarantine();
    }
    await options.api.result(canonical, {
      claimToken: claim.claimToken,
      ...result,
    });
  };

  return (act) => {
    if (attempted.has(act.commandId)) return Promise.resolve();
    const current = inFlight.get(act.commandId);
    if (current) return current;
    const run = tail.then(() => execute(act));
    tail = run.catch(() => undefined);
    inFlight.set(act.commandId, run);
    void run.finally(() => inFlight.delete(act.commandId)).catch(() => undefined);
    return run;
  };
}
