import { extractGmeetChatMessage } from './gmeet-chat.js';

let failed = 0;
const check = (name: string, condition: boolean, detail = '') => {
  console.log(`  ${condition ? '✅' : '❌'} ${name}${detail ? ` — ${detail}` : ''}`);
  if (!condition) failed++;
};

const values: Record<string, { textContent?: string; attrs?: Record<string, string> }> = {
  '[data-sender-name]': { textContent: ' Lucca ' },
  '[data-message-text]': { textContent: ' Qual foi a receita? ' },
  time: { attrs: { datetime: '2026-08-16T12:00:00Z' } },
};
const fake = {
  id: '',
  getAttribute(name: string) { return name === 'data-message-id' ? 'm-17' : null; },
  querySelector(selector: string) {
    const value = values[selector];
    if (!value) return null;
    return {
      textContent: value.textContent ?? '',
      getAttribute(name: string) { return value.attrs?.[name] ?? null; },
    };
  },
} as unknown as Element;

const message = extractGmeetChatMessage(fake);
check('extracts the visible sender', message?.sender === 'Lucca');
check('extracts normalized text', message?.text === 'Qual foi a receita?');
check('preserves the stable message id', message?.messageId === 'm-17');
check('preserves the machine timestamp', message?.timestamp === '2026-08-16T12:00:00Z');

const empty = {
  id: '', getAttribute: () => null,
  querySelector: () => null,
} as unknown as Element;
check('ignores nodes without message text', extractGmeetChatMessage(empty) === null);

if (failed) process.exit(1);
console.log('\n✅ gmeet-chat: defensive DOM extraction passes. Browser send/readback remains live-gated.');
