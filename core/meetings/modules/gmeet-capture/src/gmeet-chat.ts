/**
 * Google Meet chat capture + sender.
 *
 * Meet does not expose a supported browser API for chat. This adapter therefore uses the
 * accessibility surface (roles/labels first, CSS class fragments only as a fallback), keeps the
 * panel open while the bot is active, observes new messages and confirms every send by reading the
 * bot's own message back from the DOM. Selector drift fails closed: callers receive an explicit
 * unconfirmed result and capture keeps running.
 */

export interface GmeetChatMessage {
  sender: string;
  text: string;
  messageId?: string;
  timestamp?: string;
}

export interface GmeetChatOptions {
  botName?: string;
  log?: (message: string) => void;
  onMessage: (message: GmeetChatMessage) => void;
  pollMs?: number;
  /** Minimum delay before retrying a panel-open control that produced no surface. */
  openRetryMs?: number;
  /** Quiet window used to absorb history that Meet renders after the panel shell. */
  historySettleMs?: number;
  sendTimeoutMs?: number;
}

export interface GmeetChat {
  send(text: string): Promise<{ confirmed: boolean; reason?: string }>;
  destroy(): void;
  getState(): { panelFound: boolean; primed: boolean; seen: number; recent: GmeetChatMessage[] };
}

export const gmeetChatContainerSelectors = [
  '[role="log"][aria-live]',
  '[aria-label*="Messages"] [role="list"]',
  '[aria-label*="messages"] [role="list"]',
  '[aria-label*="Mensagens"] [role="list"]',
  '[data-chat-panel]',
] as const;

export const gmeetChatMessageSelectors = [
  '[data-message-id]',
  '[data-sender-id]',
  '[role="listitem"]',
  '[class*="chat-message"]',
] as const;

const senderSelectors = [
  '[data-sender-name]',
  '[data-self-name]',
  '[class*="sender"]',
  '[class*="author"]',
  '.poVWob',
  'span.notranslate',
] as const;

const textSelectors = [
  '[data-message-text]',
  '[class*="message-text"]',
  '.ptNLrf',
  '[dir="auto"]',
] as const;

const inputSelectors = [
  'textarea[aria-label*="Send a message"]',
  'textarea[aria-label*="send a message"]',
  'textarea[aria-label*="Enviar uma mensagem"]',
  'textarea[aria-label*="enviar uma mensagem"]',
  '[contenteditable="true"][aria-label*="message"]',
  '[contenteditable="true"][aria-label*="mensagem"]',
  '[contenteditable="true"][role="textbox"]',
] as const;

const sendButtonSelectors = [
  'button[aria-label="Send a message"]',
  'button[aria-label="Send message"]',
  'button[aria-label="Enviar uma mensagem"]',
  'button[aria-label="Enviar mensagem"]',
] as const;

const openChatLabels = new Set([
  'chat with everyone',
  'chat',
  'in-call messages',
  'mensagens na chamada',
  'mensagens com todos',
  'conversar com todos',
]);

const chatPanelLabels = new Set([
  'in-call messages',
  'mensagens na chamada',
]);

const ownSenderLabels = new Set(['you', 'você', 'voce']);
const unknownSender = 'Unknown';

const normalize = (value: string): string => value.replace(/\s+/g, ' ').trim();
const comparable = (value: string): string => normalize(value).toLocaleLowerCase();

function firstText(root: Element, selectors: readonly string[]): string {
  for (const selector of selectors) {
    const node = root.querySelector(selector);
    const value = normalize(node?.textContent ?? '');
    if (value) return value;
  }
  return '';
}

/** Parse one message node without depending on Meet's obfuscated class names. */
export function extractGmeetChatMessage(root: Element): GmeetChatMessage | null {
  const sender = normalize(root.getAttribute('data-sender-name') ?? '') || firstText(root, senderSelectors);
  const text = firstText(root, textSelectors);
  if (!text) return null;
  const messageId = root.getAttribute('data-message-id')
    ?? root.getAttribute('data-id')
    ?? root.id
    ?? undefined;
  const timestamp = root.getAttribute('data-timestamp')
    ?? root.querySelector('time')?.getAttribute('datetime')
    ?? undefined;
  return { sender: sender || unknownSender, text, messageId: messageId || undefined, timestamp };
}

function messageKey(message: GmeetChatMessage, ordinal: number): string {
  return message.messageId
    ? `id:${message.messageId}`
    : `${message.timestamp ?? ''}:${message.sender}:${message.text}:${ordinal}`;
}

function queryFirst(selectors: readonly string[], root: ParentNode = document): Element | null {
  for (const selector of selectors) {
    const node = root.querySelector(selector);
    if (node) return node;
  }
  return null;
}

function queryAll(selectors: readonly string[], root: ParentNode): Element[] {
  const found = new Set<Element>();
  for (const selector of selectors) {
    for (const node of Array.from(root.querySelectorAll(selector))) found.add(node);
  }
  return [...found];
}

/**
 * Find the chat surface without depending on Meet's obfuscated panel classes. The current Meet UI
 * exposes stable `data-message-id` values but no role/list landmark around them. In that variant,
 * walk from a real message to the nearest ancestor that also owns the composer. This keeps the
 * fallback scoped to the chat surface instead of scanning the whole meeting document.
 */
export function findGmeetChatContainer(root: ParentNode = document): Element | null {
  const landmark = queryFirst(gmeetChatContainerSelectors, root);
  const composers = queryAll(inputSelectors, root);
  const labeledPanel = deepestElement(composers
    .map(findLabeledPanelFromComposer)
    .filter((candidate): candidate is Element => candidate !== null));
  if (labeledPanel) return labeledPanel;
  if (landmark) {
    return deepestElement(composers
      .map((composer) => nearestSharedAncestor(landmark, composer))
      .filter((candidate): candidate is Element => candidate !== null)) ?? landmark;
  }
  const messages = Array.from(root.querySelectorAll('[data-message-id]'));
  if (messages.length && composers.length) {
    return deepestElement(messages
      .flatMap((message) => composers.map((composer) => nearestSharedAncestor(message, composer)))
      .filter((candidate): candidate is Element => candidate !== null));
  }
  return null;
}

function findLabeledPanelFromComposer(composer: Element): Element | null {
  let ancestor = composer.parentElement;
  for (let depth = 0; ancestor && depth < 10; depth++, ancestor = ancestor.parentElement) {
    const ownLabel = comparable(ancestor.getAttribute('aria-label') ?? '');
    if (chatPanelLabels.has(ownLabel)) return ancestor;
    const heading = Array.from(ancestor.querySelectorAll('[role="heading"], h1, h2, h3'))
      .some((node) => chatPanelLabels.has(comparable(node.textContent ?? '')));
    if (heading) return ancestor;
  }
  return null;
}

function deepestElement(elements: Element[]): Element | null {
  let best: Element | null = null;
  let bestDepth = -1;
  for (const element of elements) {
    if (element === document.body || element === document.documentElement) continue;
    let depth = 0;
    for (let cursor: Element | null = element; cursor; cursor = cursor.parentElement) depth++;
    if (depth > bestDepth) {
      best = element;
      bestDepth = depth;
    }
  }
  return best;
}

function nearestSharedAncestor(first: Element, second: Element): Element | null {
  let ancestor: Element | null = first;
  for (let depth = 0; ancestor && depth < 12; depth++, ancestor = ancestor.parentElement) {
    if (ancestor.contains(second)) return ancestor;
  }
  return null;
}

function isOpenChatLabel(rawLabel: string): boolean {
  const label = comparable(rawLabel);
  for (const expected of openChatLabels) {
    if (label === expected || label.startsWith(`${expected},`) || label.startsWith(`${expected} (`)) return true;
  }
  return false;
}

/** Find the accessible chat control, including Meet variants that use role=button. */
export function findGmeetChatOpener(root: ParentNode = document): HTMLElement | null {
  const selector = [
    'button[aria-label]', 'button[title]',
    '[role="button"][aria-label]', '[role="button"][title]',
  ].join(', ');
  for (const control of Array.from(root.querySelectorAll<HTMLElement>(selector))) {
    const labels = [control.getAttribute('aria-label') ?? '', control.getAttribute('title') ?? ''];
    if (labels.some(isOpenChatLabel)) return control;
  }
  return null;
}

function setInputValue(input: Element, text: string): boolean {
  if (input instanceof HTMLTextAreaElement || input instanceof HTMLInputElement) {
    const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    input.focus();
    if (setter) setter.call(input, text);
    else input.value = text;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  }
  if (input instanceof HTMLElement && input.isContentEditable) {
    input.focus();
    input.textContent = text;
    input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
    return true;
  }
  return false;
}

export function createGmeetChat(options: GmeetChatOptions): GmeetChat {
  const log = options.log ?? (() => {});
  const botName = comparable(options.botName ?? '');
  const seenNodes = new WeakSet<Element>();
  const seenKeys = new Set<string>();
  const recent: GmeetChatMessage[] = [];
  let panel: Element | null = null;
  let destroyed = false;
  let primed = false;
  let primeStartedAt: number | null = null;
  let lastPrimeChangeAt = 0;
  let lastOpenRequestAt = 0;
  let unavailableReported = false;
  const openRetryMs = Math.max(0, options.openRetryMs ?? 3000);
  const historySettleMs = Math.max(0, options.historySettleMs ?? 500);

  const isOwn = (message: GmeetChatMessage): boolean => {
    const sender = comparable(message.sender);
    return botName !== '' && (sender === botName || ownSenderLabels.has(sender));
  };

  const ensurePanel = (): Element | null => {
    if (panel?.isConnected) return panel;
    panel = findGmeetChatContainer();
    if (panel) {
      lastOpenRequestAt = 0;
      unavailableReported = false;
      return panel;
    }
    const open = findGmeetChatOpener();
    const alreadyOpen = open?.getAttribute('aria-pressed') === 'true'
      || open?.getAttribute('aria-expanded') === 'true';
    const now = Date.now();
    if (open && !alreadyOpen && (lastOpenRequestAt === 0 || now - lastOpenRequestAt >= openRetryMs)) {
      lastOpenRequestAt = now;
      open.click();
      log('chat panel open requested');
    }
    panel = findGmeetChatContainer();
    if (panel) lastOpenRequestAt = 0;
    if (!panel && !unavailableReported) {
      unavailableReported = true;
      log('chat panel not available yet');
    }
    return panel;
  };

  const scan = (includeOwn = false): GmeetChatMessage[] => {
    const container = ensurePanel();
    if (!container) return [];
    const scanStartedAt = Date.now();
    if (primeStartedAt === null) primeStartedAt = scanStartedAt;
    const found: GmeetChatMessage[] = [];
    let inheritedSender = '';
    let newlySeen = 0;
    for (const selector of gmeetChatMessageSelectors) {
      const nodes = Array.from(container.querySelectorAll(selector));
      if (!nodes.length) continue;
      nodes.forEach((node, ordinal) => {
        const extracted = extractGmeetChatMessage(node);
        if (!extracted) return;
        const message = extracted.sender === unknownSender && inheritedSender
          ? { ...extracted, sender: inheritedSender }
          : extracted;
        if (message.sender !== unknownSender) inheritedSender = message.sender;
        const own = isOwn(message);
        if (includeOwn) found.push(message);
        if (seenNodes.has(node)) return;
        seenNodes.add(node);
        const key = messageKey(message, ordinal);
        if (seenKeys.has(key)) return;
        seenKeys.add(key);
        newlySeen++;
        recent.push(message);
        if (recent.length > 30) recent.shift();
        if (primed && !own) {
          log(`chat ${message.sender}: ${message.text.slice(0, 60)}`);
          try { options.onMessage(message); } catch { /* chat cannot break audio capture */ }
        }
      });
      break;
    }
    if (!primed && newlySeen > 0) lastPrimeChangeAt = scanStartedAt;
    const quietSince = Math.max(primeStartedAt, lastPrimeChangeAt);
    if (!primed && scanStartedAt - quietSince >= historySettleMs) {
      primed = true;
      log(`chat reader primed with ${seenKeys.size} existing message(s)`);
    }
    return found;
  };

  // Prime existing history so joining a meeting cannot replay old questions to the agent.
  scan();
  const poll = globalThis.setInterval(scan, options.pollMs ?? 750);

  return {
    async send(rawText) {
      const text = normalize(rawText);
      if (!text) return { confirmed: false, reason: 'empty_message' };
      if (destroyed) return { confirmed: false, reason: 'chat_destroyed' };
      if (!botName) return { confirmed: false, reason: 'composer_not_found' };
      const container = ensurePanel();
      if (!container) return { confirmed: false, reason: 'composer_not_found' };
      const input = queryFirst(inputSelectors, container);
      if (!input || !setInputValue(input, text)) return { confirmed: false, reason: 'composer_not_found' };
      const before = scan(true).filter((message) => isOwn(message) && message.text === text).length;
      const sendButton = queryFirst(sendButtonSelectors, container) as HTMLButtonElement | null;
      if (sendButton && !sendButton.disabled) sendButton.click();
      else input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true }));

      const deadline = Date.now() + (options.sendTimeoutMs ?? 5000);
      while (!destroyed) {
        if (Date.now() > deadline) break;
        const count = scan(true).filter((message) => isOwn(message) && message.text === text).length;
        if (count > before) return { confirmed: true };
        const remaining = deadline - Date.now();
        if (remaining <= 0) break;
        await new Promise((resolve) => globalThis.setTimeout(resolve, Math.min(100, remaining)));
      }
      return { confirmed: false, reason: 'message_not_observed_after_send' };
    },
    destroy() {
      destroyed = true;
      globalThis.clearInterval(poll);
    },
    getState() {
      return { panelFound: Boolean(panel?.isConnected), primed, seen: seenKeys.size, recent: recent.slice(-10) };
    },
  };
}
