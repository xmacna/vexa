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
  sendTimeoutMs?: number;
}

export interface GmeetChat {
  send(text: string): Promise<{ confirmed: boolean; reason?: string }>;
  destroy(): void;
  getState(): { panelFound: boolean; seen: number; recent: GmeetChatMessage[] };
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
  'span.notranslate',
] as const;

const textSelectors = [
  '[data-message-text]',
  '[class*="message-text"]',
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
  'mensagens na chamada',
  'mensagens com todos',
  'conversar com todos',
]);

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
  const sender = firstText(root, senderSelectors);
  const text = firstText(root, textSelectors);
  if (!text) return null;
  const messageId = root.getAttribute('data-message-id')
    ?? root.getAttribute('data-id')
    ?? root.id
    ?? undefined;
  const timestamp = root.getAttribute('data-timestamp')
    ?? root.querySelector('time')?.getAttribute('datetime')
    ?? undefined;
  return { sender: sender || 'Unknown', text, messageId: messageId || undefined, timestamp };
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

function findOpenChatButton(): HTMLButtonElement | null {
  for (const button of Array.from(document.querySelectorAll<HTMLButtonElement>('button[aria-label], button[title]'))) {
    const label = comparable(button.getAttribute('aria-label') ?? button.getAttribute('title') ?? '');
    if (openChatLabels.has(label)) return button;
  }
  return null;
}

function setInputValue(input: Element, text: string): boolean {
  if (input instanceof HTMLTextAreaElement || input instanceof HTMLInputElement) {
    const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
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

  const ensurePanel = (): Element | null => {
    if (panel?.isConnected) return panel;
    panel = queryFirst(gmeetChatContainerSelectors);
    if (panel) return panel;
    const open = findOpenChatButton();
    if (open && open.getAttribute('aria-pressed') !== 'true') open.click();
    panel = queryFirst(gmeetChatContainerSelectors);
    return panel;
  };

  const scan = (includeOwn = false): GmeetChatMessage[] => {
    const container = ensurePanel();
    if (!container) return [];
    const found: GmeetChatMessage[] = [];
    for (const selector of gmeetChatMessageSelectors) {
      const nodes = Array.from(container.querySelectorAll(selector));
      if (!nodes.length) continue;
      nodes.forEach((node, ordinal) => {
        const message = extractGmeetChatMessage(node);
        if (!message) return;
        const own = botName !== '' && comparable(message.sender) === botName;
        if (includeOwn) found.push(message);
        if (seenNodes.has(node)) return;
        seenNodes.add(node);
        const key = messageKey(message, ordinal);
        if (seenKeys.has(key)) return;
        seenKeys.add(key);
        recent.push(message);
        if (recent.length > 30) recent.shift();
        if (!own) {
          log(`chat ${message.sender}: ${message.text.slice(0, 60)}`);
          try { options.onMessage(message); } catch { /* chat cannot break audio capture */ }
        }
      });
      break;
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
      ensurePanel();
      const input = queryFirst(inputSelectors);
      if (!input || !setInputValue(input, text)) return { confirmed: false, reason: 'composer_not_found' };
      const before = scan(true).filter((message) => comparable(message.sender) === botName && message.text === text).length;
      const sendButton = queryFirst(sendButtonSelectors) as HTMLButtonElement | null;
      if (sendButton && !sendButton.disabled) sendButton.click();
      else input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true }));

      const deadline = Date.now() + (options.sendTimeoutMs ?? 5000);
      while (!destroyed && Date.now() < deadline) {
        await new Promise((resolve) => globalThis.setTimeout(resolve, 100));
        const count = scan(true).filter((message) => comparable(message.sender) === botName && message.text === text).length;
        if (count > before) return { confirmed: true };
      }
      return { confirmed: false, reason: 'message_not_observed_after_send' };
    },
    destroy() {
      destroyed = true;
      globalThis.clearInterval(poll);
    },
    getState() {
      return { panelFound: Boolean(panel?.isConnected), seen: seenKeys.size, recent: recent.slice(-10) };
    },
  };
}
