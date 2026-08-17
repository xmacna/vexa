import { JSDOM } from 'jsdom';
import {
  createGmeetChat,
  extractGmeetChatMessage,
  findGmeetChatContainer,
} from './gmeet-chat.js';

let failed = 0;
const check = (name: string, condition: boolean, detail = '') => {
  console.log(`  ${condition ? '✅' : '❌'} ${name}${detail ? ` — ${detail}` : ''}`);
  if (!condition) failed++;
};

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

const messageHtml = (id: string, text: string, sender?: string) => `
  <div data-message-id="${id}">
    ${sender ? `<div class="poVWob"><span class="notranslate" dir="auto">${sender}</span></div>` : ''}
    <div class="ptNLrf"><div dir="auto">${text}</div></div>
  </div>`;

const panelHtml = (messages: string) => `
  <section id="current-chat-surface">
    <header><h2>In-call messages</h2></header>
    <div id="messages">${messages}</div>
    <textarea aria-label="Send a message"></textarea>
    <button aria-label="Send a message">Send</button>
  </section>`;

const installDom = (html: string) => {
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`, {
    url: 'https://meet.google.com/fixture-room',
  });
  const keys = [
    'document', 'Element', 'HTMLElement', 'HTMLTextAreaElement', 'HTMLInputElement',
    'Event', 'InputEvent', 'KeyboardEvent',
  ] as const;
  const previous = new Map<string, PropertyDescriptor | undefined>();
  for (const key of keys) {
    previous.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, {
      configurable: true,
      writable: true,
      value: key === 'document' ? dom.window.document : dom.window[key],
    });
  }
  return {
    document: dom.window.document,
    restore() {
      dom.window.close();
      for (const key of keys) {
        const descriptor = previous.get(key);
        if (descriptor) Object.defineProperty(globalThis, key, descriptor);
        else Reflect.deleteProperty(globalThis, key);
      }
    },
  };
};

{
  const fixture = installDom(messageHtml('m-17', 'Qual foi a receita?', 'Ana'));
  const root = fixture.document.querySelector('[data-message-id]')!;
  root.insertAdjacentHTML('beforeend', '<time datetime="2026-08-16T12:00:00Z"></time>');
  const message = extractGmeetChatMessage(root);
  check('extracts the sender from the observed Meet group header', message?.sender === 'Ana');
  check('does not confuse the sender dir=auto with message text', message?.text === 'Qual foi a receita?');
  check('preserves the stable message id', message?.messageId === 'm-17');
  check('preserves the machine timestamp', message?.timestamp === '2026-08-16T12:00:00Z');
  check('ignores nodes without message text',
    extractGmeetChatMessage(fixture.document.createElement('div')) === null);
  fixture.restore();
}

{
  const fixture = installDom('<button id="retry" aria-label="Chat with everyone"></button>');
  let clicks = 0;
  fixture.document.querySelector<HTMLButtonElement>('#retry')!.onclick = () => {
    clicks++;
    if (clicks === 2) fixture.document.body.insertAdjacentHTML('beforeend', panelHtml(''));
  };
  const chat = createGmeetChat({
    botName: 'Marvin', pollMs: 5, openRetryMs: 10, historySettleMs: 0, onMessage: () => {},
  });
  await wait(35);
  check('retries an opener click that did not produce a panel',
    clicks === 2 && chat.getState().panelFound, `${clicks}:${chat.getState().panelFound}`);
  chat.destroy();
  fixture.restore();
}

{
  const fixture = installDom(`
    <article id="unrelated-widget">
      <div data-message-id="unrelated"><div class="ptNLrf">Notification</div></div>
      <textarea aria-label="Send a message"></textarea>
    </article>
    ${panelHtml(messageHtml('m-1', 'Mensagem existente', 'Ana'))}`);
  check('finds the current Meet surface despite an unrelated data-message-id elsewhere in the page',
    findGmeetChatContainer(fixture.document)?.id === 'current-chat-surface');
  fixture.restore();
}

{
  const fixture = installDom(`
    ${messageHtml('decoy', 'Not a scoped chat message', 'Elsewhere')}
    <textarea aria-label="Send a message"></textarea>`);
  check('fails closed instead of treating the whole document as a chat surface',
    findGmeetChatContainer(fixture.document) === null);
  fixture.restore();
}

{
  const fixture = installDom('<div id="chat-opener" role="button" aria-label="In-call messages"></div>');
  let openClicks = 0;
  fixture.document.querySelector<HTMLElement>('#chat-opener')!.onclick = () => {
    openClicks++;
    fixture.document.body.insertAdjacentHTML('beforeend', panelHtml(''));
    setTimeout(() => {
      fixture.document.querySelector('#messages')!.insertAdjacentHTML(
        'beforeend', messageHtml('old-1', 'Pergunta anterior à entrada do bot', 'Ana'));
    }, 5);
  };
  const received: Array<{ sender: string; text: string }> = [];
  const logs: string[] = [];
  const chat = createGmeetChat({
    botName: 'Marvin',
    pollMs: 5,
    historySettleMs: 15,
    log: (line) => logs.push(line),
    onMessage: (message) => received.push(message),
  });
  await wait(50);
  check('opens the panel from its accessible aria label', openClicks === 1, String(openClicks));
  check('does not replay history loaded asynchronously after opening the panel', received.length === 0,
    JSON.stringify(received));

  fixture.document.querySelector('#messages')!.insertAdjacentHTML(
    'beforeend',
    messageHtml('new-1', 'Marvin, responda esta nova pergunta'),
  );
  await wait(35);
  check('captures a new message in the current Meet DOM', received.length === 1,
    JSON.stringify(received));
  check('inherits the visible sender for a grouped follow-up message', received[0]?.sender === 'Ana',
    received[0]?.sender);
  check('reports that the chat reader was primed', chat.getState().primed === true
    && logs.some((line) => line.includes('primed')));
  chat.destroy();
  fixture.restore();
}

{
  const fixture = installDom(`
    <button id="reopen" aria-label="Chat with everyone, 2 unread messages" aria-pressed="false"></button>
    ${panelHtml(messageHtml('stable-old', 'Já observada', 'Ana'))}`);
  let reopenClicks = 0;
  fixture.document.querySelector<HTMLButtonElement>('#reopen')!.onclick = () => {
    reopenClicks++;
    fixture.document.body.insertAdjacentHTML(
      'beforeend',
      panelHtml([
        messageHtml('stable-old', 'Já observada', 'Ana'),
        messageHtml('after-reopen', 'Nova depois da reabertura'),
      ].join('')),
    );
  };
  const received: Array<{ sender: string; text: string }> = [];
  const chat = createGmeetChat({
    botName: 'Marvin', pollMs: 5, historySettleMs: 0,
    onMessage: (message) => received.push(message),
  });
  await wait(10);
  fixture.document.querySelector('#current-chat-surface')!.remove();
  await wait(25);
  check('reopens the panel if Meet detaches it while the bot remains active', reopenClicks === 1,
    String(reopenClicks));
  check('does not replay stable ids after the panel is re-rendered',
    received.length === 1 && received[0]?.text === 'Nova depois da reabertura', JSON.stringify(received));
  chat.destroy();
  fixture.restore();
}

{
  const fixture = installDom(`
    <textarea id="decoy" aria-label="Send a message"></textarea>
    ${panelHtml(messageHtml('own-old', 'Estado atual', 'You'))}`);
  const panelInput = fixture.document.querySelector<HTMLTextAreaElement>('#current-chat-surface textarea')!;
  fixture.document.querySelector<HTMLButtonElement>('#current-chat-surface button')!.onclick = () => {
    fixture.document.querySelector('#messages')!.insertAdjacentHTML(
      'beforeend',
      messageHtml('own-new', panelInput.value, 'You'),
    );
    panelInput.value = '';
  };
  const chat = createGmeetChat({ botName: 'Marvin', pollMs: 5, sendTimeoutMs: 80, onMessage: () => {} });
  await wait(10);
  const result = await chat.send('Estado atual');
  check('sends through the composer scoped to the detected chat surface',
    result.confirmed && fixture.document.querySelector<HTMLTextAreaElement>('#decoy')!.value === '',
    JSON.stringify(result));
  check('confirms only after a new own message is read back from the DOM', result.confirmed === true,
    JSON.stringify(result));
  chat.destroy();
  fixture.restore();
}

{
  const fixture = installDom(panelHtml(messageHtml('old', 'Anterior', 'Ana')));
  fixture.document.querySelector<HTMLButtonElement>('button')!.onclick = () => {
    const input = fixture.document.querySelector<HTMLTextAreaElement>('textarea')!;
    const text = input.value;
    input.value = '';
    setTimeout(() => fixture.document.querySelector('#messages')!.insertAdjacentHTML(
      'beforeend', messageHtml('too-late', text, 'You')), 50);
  };
  const chat = createGmeetChat({ botName: 'Marvin', pollMs: 5, sendTimeoutMs: 25, onMessage: () => {} });
  await wait(10);
  const result = await chat.send('Não deve parecer confirmado');
  check('fails closed when the own DOM message appears only after the confirmation deadline',
    result.confirmed === false && result.reason === 'message_not_observed_after_send',
    JSON.stringify(result));
  await wait(35);
  chat.destroy();
  fixture.restore();
}

if (failed) process.exit(1);
console.log('\n✅ gmeet-chat: current DOM capture, history priming, scoped send and readback pass.');
