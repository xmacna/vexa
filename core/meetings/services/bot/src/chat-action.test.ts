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

if (failed) process.exit(1);
console.log('\n✅ chat-action: acts.v1 chat_send is confirmed by the browser adapter.');
