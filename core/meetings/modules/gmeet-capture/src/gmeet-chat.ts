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
  getState(): {
    panelFound: boolean;
    primed: boolean;
    composerFound: boolean;
    seen: number;
    recent: GmeetChatMessage[];
  };
}

/**
 * Browser-side timing contract. The control-plane readiness probe must outlive one opener retry
 * plus the history quiet window and a full poll tick; see CONFIRMED_CHAT_BRIDGE_PROBE_DEFAULTS in
 * the bot composition root.
 */
export const GMEET_CHAT_DEFAULTS = Object.freeze({
  pollMs: 750,
  openRetryMs: 3_000,
  historySettleMs: 500,
  sendTimeoutMs: 5_000,
});

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
const maxPreOpenCloseAttempts = 2;

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
 * Find the chat surface without depending on Meet's obfuscated panel classes. An explicit chat
 * landmark or a panel labelled by its accessibility surface is authoritative. The observed Meet
 * DOM can omit both. A snapshot taken immediately before clicking the opener authorizes only its
 * newly mounted subtree. An unlabeled panel that predates this bridge must be closed and reopened
 * to establish the same causal boundary; structure alone never authorizes a pre-existing shell.
 */
export function findGmeetChatContainer(
  root: ParentNode = document,
  elementsBeforeOpen?: ReadonlySet<Element>,
): Element | null {
  const composers = queryAll(inputSelectors, root).filter(isElementExposed);
  const labeledPanel = deepestElement(composers
    .map(findLabeledPanelFromComposer)
    .filter((candidate): candidate is Element => candidate !== null && isElementExposed(candidate)));
  if (labeledPanel) return labeledPanel;
  const landmark = deepestElement(queryAll(gmeetChatContainerSelectors, root)
    .filter((landmark) => isElementExposed(landmark)
      && composers.some((composer) => landmark.contains(composer))));
  if (landmark) return landmark;
  if (elementsBeforeOpen) {
    const transitioned = findNewPanelFromOpenerTransition(root, composers, elementsBeforeOpen);
    if (transitioned) return transitioned;
  }
  return null;
}

function isElementExposed(element: Element): boolean {
  if (!element.isConnected) return false;
  for (let cursor: Element | null = element; cursor; cursor = cursor.parentElement) {
    if (cursor.hasAttribute('hidden')
      || comparable(cursor.getAttribute('aria-hidden') ?? '') === 'true') return false;
    const style = cursor.ownerDocument.defaultView?.getComputedStyle(cursor);
    if (style?.display === 'none' || style?.visibility === 'hidden' || style?.visibility === 'collapse') return false;
  }
  return true;
}

function findNewPanelFromOpenerTransition(
  root: ParentNode,
  composers: Element[],
  elementsBeforeOpen: ReadonlySet<Element>,
): Element | null {
  const candidates: Element[] = [];
  for (const composer of composers) {
    if (elementsBeforeOpen.has(composer) || !isElementExposed(composer)) continue;
    let ancestor = composer.parentElement;
    let newSubtreeRoot: Element | null = null;
    let messageBoundary: Element | null = null;
    while (ancestor && !elementsBeforeOpen.has(ancestor)) {
      if (ancestor === document.body || ancestor === document.documentElement) break;
      newSubtreeRoot = ancestor;
      if (!messageBoundary && ancestor.querySelector('[data-message-id]')) messageBoundary = ancestor;
      if (ancestor === root) break;
      ancestor = ancestor.parentElement;
    }
    // With messages present, the nearest new common ancestor is the narrowest useful boundary.
    // An empty panel has no second anchor, so its newly mounted subtree root is the only honest
    // boundary. A composer inserted directly into an old shell yields no candidate and fails closed.
    const candidate = messageBoundary ?? newSubtreeRoot;
    if (candidate) candidates.push(candidate);
  }
  return deepestElement(candidates);
}

function snapshotElements(root: ParentNode = document): ReadonlySet<Element> {
  const elements = new Set<Element>(Array.from(root.querySelectorAll('*')));
  if (root instanceof Element) elements.add(root);
  return elements;
}

function snapshotMessageIds(root: ParentNode = document): ReadonlySet<string> {
  return new Set(Array.from(root.querySelectorAll('[data-message-id]'))
    .map((message) => message.getAttribute('data-message-id') ?? '')
    .filter(Boolean));
}

function findLabeledPanelFromComposer(composer: Element): Element | null {
  let ancestor = composer.parentElement;
  for (let depth = 0; ancestor && depth < 10; depth++, ancestor = ancestor.parentElement) {
    const ownLabel = comparable(ancestor.getAttribute('aria-label') ?? '');
    if (chatPanelLabels.has(ownLabel)) return ancestor;
    const heading = Array.from(ancestor.querySelectorAll('[role="heading"], h1, h2, h3'))
      .some((node) => isElementExposed(node)
        && chatPanelLabels.has(comparable(node.textContent ?? '')));
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

function isOpenChatLabel(rawLabel: string): boolean {
  const label = comparable(rawLabel);
  for (const expected of openChatLabels) {
    if (label === expected || label.startsWith(`${expected},`) || label.startsWith(`${expected} (`)) return true;
  }
  return false;
}

function isOpenChatControl(control: Element): boolean {
  return control.getAttribute('aria-pressed') === 'true'
    || control.getAttribute('aria-expanded') === 'true';
}

/** Find the accessible chat control, including Meet variants that use role=button. */
export function findGmeetChatOpener(root: ParentNode = document): HTMLElement | null {
  const selector = [
    'button[aria-label]', 'button[title]',
    '[role="button"][aria-label]', '[role="button"][title]',
  ].join(', ');
  for (const control of Array.from(root.querySelectorAll<HTMLElement>(selector))) {
    if (!isElementExposed(control)) continue;
    const labels = [control.getAttribute('aria-label') ?? '', control.getAttribute('title') ?? ''];
    if (labels.some(isOpenChatLabel)) return control;
  }
  return null;
}

function setInputValue(input: Element, text: string): boolean {
  if (!isElementExposed(input)) return false;
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

function usableComposer(root: ParentNode): Element | null {
  for (const input of queryAll(inputSelectors, root)) {
    if (!isElementExposed(input)) continue;
    if (input instanceof HTMLTextAreaElement || input instanceof HTMLInputElement) {
      if (!input.disabled && !input.readOnly) return input;
      continue;
    }
    if (input instanceof HTMLElement && input.isContentEditable
      && input.getAttribute('aria-disabled') !== 'true') return input;
  }
  return null;
}

function timestampMilliseconds(value?: string): number | null {
  if (!value) return null;
  if (/^\d+$/.test(value)) {
    const numeric = Number(value);
    if (!Number.isSafeInteger(numeric)) return null;
    return numeric < 10_000_000_000 ? numeric * 1_000 : numeric;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function containsComparableName(text: string, name: string): boolean {
  if (!name) return false;
  const value = comparable(text);
  const word = (character: string | undefined): boolean => Boolean(character && /[\p{L}\p{N}_]/u.test(character));
  for (let from = 0; from <= value.length - name.length;) {
    const index = value.indexOf(name, from);
    if (index < 0) return false;
    if (!word(value[index - 1]) && !word(value[index + name.length])) return true;
    from = index + name.length;
  }
  return false;
}

export function createGmeetChat(options: GmeetChatOptions): GmeetChat {
  const log = options.log ?? (() => {});
  const botName = comparable(options.botName ?? '');
  const botMentionName = comparable((options.botName ?? '').split(/[·—]/u, 1)[0] ?? '');
  const seenNodes = new WeakSet<Element>();
  const seenKeys = new Set<string>();
  const recent: GmeetChatMessage[] = [];
  let panel: Element | null = null;
  let destroyed = false;
  let primed = false;
  let primeStartedAt: number | null = null;
  let lastPrimeChangeAt = 0;
  let lastOpenRequestAt = 0;
  let lastCloseRequestAt = 0;
  let preOpenCloseAttempts = 0;
  let elementsBeforeOpen: ReadonlySet<Element> | null = null;
  let messageIdsBeforePreOpenRecovery: ReadonlySet<string> | null = null;
  let unavailableReported = false;
  const primeCutoffAt = Date.now();
  const liveDuringPrime: GmeetChatMessage[] = [];
  const pollMs = Math.max(1, options.pollMs ?? GMEET_CHAT_DEFAULTS.pollMs);
  const openRetryMs = Math.max(0, options.openRetryMs ?? GMEET_CHAT_DEFAULTS.openRetryMs);
  const historySettleMs = Math.max(0, options.historySettleMs ?? GMEET_CHAT_DEFAULTS.historySettleMs);

  const isOwn = (message: GmeetChatMessage): boolean => {
    const sender = comparable(message.sender);
    return botName !== '' && (sender === botName || ownSenderLabels.has(sender));
  };

  const ensurePanel = (): Element | null => {
    if (panel?.isConnected && isElementExposed(panel) && usableComposer(panel)) return panel;
    panel = findGmeetChatContainer(document, elementsBeforeOpen ?? undefined);
    if (panel) {
      elementsBeforeOpen = null;
      lastOpenRequestAt = 0;
      lastCloseRequestAt = 0;
      preOpenCloseAttempts = 0;
      unavailableReported = false;
      return panel;
    }
    const open = findGmeetChatOpener();
    const alreadyOpen = open ? isOpenChatControl(open) : false;
    const now = Date.now();
    const awaitingOpenMount = lastOpenRequestAt !== 0 && now - lastOpenRequestAt < openRetryMs;
    if (open && alreadyOpen && !awaitingOpenMount
      && preOpenCloseAttempts < maxPreOpenCloseAttempts
      && (lastCloseRequestAt === 0 || now - lastCloseRequestAt >= openRetryMs)) {
      if (preOpenCloseAttempts === 0) messageIdsBeforePreOpenRecovery = snapshotMessageIds();
      lastCloseRequestAt = now;
      lastOpenRequestAt = 0;
      preOpenCloseAttempts++;
      elementsBeforeOpen = null;
      open.click();
      log('unlabeled pre-open chat panel close requested before re-opening');
    } else if (open && !alreadyOpen
      && (lastOpenRequestAt === 0 || now - lastOpenRequestAt >= openRetryMs)) {
      elementsBeforeOpen = snapshotElements();
      lastOpenRequestAt = now;
      lastCloseRequestAt = 0;
      open.click();
      log('chat panel open requested');
    }
    panel = findGmeetChatContainer(document, elementsBeforeOpen ?? undefined);
    if (panel) {
      elementsBeforeOpen = null;
      lastOpenRequestAt = 0;
      lastCloseRequestAt = 0;
      preOpenCloseAttempts = 0;
    }
    if (!panel && !unavailableReported) {
      unavailableReported = true;
      log('chat panel not available yet');
    }
    return panel;
  };

  const deliver = (message: GmeetChatMessage): void => {
    log(`chat ${message.sender}: ${message.text.slice(0, 60)}`);
    try { options.onMessage(message); } catch { /* chat cannot break audio capture */ }
  };

  const scan = (includeOwn = false): GmeetChatMessage[] => {
    const container = ensurePanel();
    if (!container) return [];
    if (primed) messageIdsBeforePreOpenRecovery = null;
    const scanStartedAt = Date.now();
    const firstPrimeScan = primeStartedAt === null;
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
        if (primed && !own) deliver(message);
        else if (!primed && !own) {
          const timestamp = timestampMilliseconds(message.timestamp);
          const directedMention = containsComparableName(message.text, botMentionName);
          const arrivedDuringPreOpenRecovery = Boolean(message.messageId
            && messageIdsBeforePreOpenRecovery
            && !messageIdsBeforePreOpenRecovery.has(message.messageId));
          if (timestamp !== null ? timestamp >= primeCutoffAt
            : directedMention && (arrivedDuringPreOpenRecovery || !firstPrimeScan)) {
            // Untimestamped late history and a live arrival are indistinguishable in Meet's DOM.
            // Buffer only explicit bot mentions: one historical mention may be delivered once, but
            // stable message keys prevent repeats and losing a new directed request is worse.
            liveDuringPrime.push(message);
          }
        }
      });
      break;
    }
    if (!primed && newlySeen > 0) lastPrimeChangeAt = scanStartedAt;
    const quietSince = Math.max(primeStartedAt, lastPrimeChangeAt);
    if (!primed && scanStartedAt - quietSince >= historySettleMs) {
      primed = true;
      messageIdsBeforePreOpenRecovery = null;
      log(`chat reader primed with ${seenKeys.size} existing message(s)`);
      for (const message of liveDuringPrime.splice(0)) deliver(message);
    }
    return found;
  };

  // Prime existing history so joining a meeting cannot replay old questions to the agent.
  scan();
  const poll = globalThis.setInterval(scan, pollMs);

  return {
    async send(rawText) {
      const text = normalize(rawText);
      if (!text) return { confirmed: false, reason: 'empty_message' };
      if (destroyed) return { confirmed: false, reason: 'chat_destroyed' };
      if (!botName) return { confirmed: false, reason: 'composer_not_found' };
      const container = ensurePanel();
      if (!container) return { confirmed: false, reason: 'composer_not_found' };
      const input = usableComposer(container);
      if (!input || !setInputValue(input, text)) return { confirmed: false, reason: 'composer_not_found' };
      const before = scan(true).filter((message) => isOwn(message) && message.text === text).length;
      const sendButton = queryFirst(sendButtonSelectors, container) as HTMLButtonElement | null;
      if (sendButton && !sendButton.disabled) sendButton.click();
      else input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true }));

      const deadline = Date.now() + (options.sendTimeoutMs ?? GMEET_CHAT_DEFAULTS.sendTimeoutMs);
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
      const connectedPanel = panel?.isConnected && isElementExposed(panel) ? panel : null;
      return {
        panelFound: Boolean(connectedPanel),
        primed,
        composerFound: Boolean(connectedPanel && usableComposer(connectedPanel)),
        seen: seenKeys.size,
        recent: recent.slice(-10),
      };
    },
  };
}
