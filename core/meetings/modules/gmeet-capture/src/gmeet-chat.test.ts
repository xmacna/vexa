import { JSDOM } from 'jsdom';
import {
  createGmeetChat,
  extractGmeetChatMessage,
  findGmeetChatContainer,
  GMEET_CHAT_DEFAULTS,
} from './gmeet-chat.js';

let failed = 0;
const check = (name: string, condition: boolean, detail = '') => {
  console.log(`  ${condition ? '✅' : '❌'} ${name}${detail ? ` — ${detail}` : ''}`);
  if (!condition) failed++;
};

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

const messageHtml = (id: string, text: string, sender?: string, timestamp?: string) => `
  <div data-message-id="${id}"${timestamp ? ` data-timestamp="${timestamp}"` : ''}>
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

// Sanitized from the pilot DOM: the panel is mounted by the opener without a heading, landmark,
// aria-label or stable class. data-message-id and the scoped composer are the durable surface.
const observedUnlabeledPanelHtml = (messages: string, id = 'observed-chat-surface') => `
  <section id="${id}">
    <div data-chat-messages>${messages}</div>
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
    if (clicks === 2) fixture.document.body.insertAdjacentHTML(
      'beforeend', observedUnlabeledPanelHtml('', 'retry-observed-chat-surface'));
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
  const fixture = installDom('<button id="default-retry" aria-label="Chat with everyone"></button>');
  let clicks = 0;
  fixture.document.querySelector<HTMLButtonElement>('#default-retry')!.onclick = () => {
    clicks++;
    if (clicks === 2) fixture.document.body.insertAdjacentHTML(
      'beforeend', observedUnlabeledPanelHtml('', 'default-retry-observed-chat-surface'));
  };
  const startedAt = Date.now();
  const chat = createGmeetChat({ botName: 'Marvin', onMessage: () => {} });
  const testDeadline = startedAt + GMEET_CHAT_DEFAULTS.openRetryMs
    + (GMEET_CHAT_DEFAULTS.pollMs * 3) + GMEET_CHAT_DEFAULTS.historySettleMs;
  let state = chat.getState();
  while (!(state.panelFound && state.primed && state.composerFound) && Date.now() < testDeadline) {
    await wait(50);
    state = chat.getState();
  }
  check('default timing reaches a primed scoped composer after a second opener click',
    clicks === 2 && state.panelFound && state.primed && state.composerFound
      && Date.now() <= testDeadline,
    `${clicks}:${JSON.stringify(state)}`);
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
  const fixture = installDom(`
    <main id="meet-app-shell">
      <div role="log" aria-live="polite">
        ${messageHtml('shell-decoy', 'Generic application notification', 'Elsewhere')}
      </div>
      <textarea aria-label="Send a message"></textarea>
    </main>`);
  const shellComposer = fixture.document.querySelector<HTMLTextAreaElement>('textarea')!;
  const chat = createGmeetChat({ botName: 'Marvin', pollMs: 5, historySettleMs: 0, onMessage: () => {} });
  const send = await chat.send('must not reach the app shell');
  check('does not cache a landmark and sibling composer app shell as the chat surface',
    findGmeetChatContainer(fixture.document) === null && chat.getState().panelFound === false
      && chat.getState().composerFound === false && shellComposer.value === '' && send.confirmed === false,
    `${JSON.stringify(chat.getState())}:${JSON.stringify(send)}:${shellComposer.value}`);
  chat.destroy();
  fixture.restore();
}

for (const [name, wrapper] of [
  ['hidden attribute', (panel: string) => `<div hidden>${panel}</div>`],
  ['aria-hidden ancestor', (panel: string) => `<div aria-hidden="true">${panel}</div>`],
  ['display none ancestor', (panel: string) => `<div style="display:none">${panel}</div>`],
  ['visibility hidden ancestor', (panel: string) => `<div style="visibility:hidden">${panel}</div>`],
] as const) {
  const fixture = installDom(wrapper(panelHtml(messageHtml(`hidden-${name}`, 'Hidden history', 'Ana'))));
  let clicks = 0;
  fixture.document.querySelector<HTMLButtonElement>('button')!.onclick = () => { clicks++; };
  const input = fixture.document.querySelector<HTMLTextAreaElement>('textarea')!;
  const chat = createGmeetChat({
    botName: 'Marvin', pollMs: 5, historySettleMs: 0, sendTimeoutMs: 20, onMessage: () => {},
  });
  const sent = await chat.send('must not enter a hidden explicit panel');
  check(`does not authorize an explicit labeled panel under ${name}`,
    findGmeetChatContainer(fixture.document) === null && chat.getState().panelFound === false
      && chat.getState().composerFound === false && clicks === 0 && input.value === ''
      && sent.confirmed === false,
    `${JSON.stringify(chat.getState())}:${clicks}:${input.value}:${JSON.stringify(sent)}`);
  chat.destroy();
  fixture.restore();
}

{
  const fixture = installDom(`<div id="visibility-host">${panelHtml('')}</div>`);
  let clicks = 0;
  fixture.document.querySelector<HTMLButtonElement>('button')!.onclick = () => { clicks++; };
  const input = fixture.document.querySelector<HTMLTextAreaElement>('textarea')!;
  const chat = createGmeetChat({
    botName: 'Marvin', pollMs: 5, historySettleMs: 0, sendTimeoutMs: 20, onMessage: () => {},
  });
  await wait(10);
  check('visible explicit panel is initially ready before visibility revocation',
    chat.getState().panelFound && chat.getState().primed && chat.getState().composerFound,
    JSON.stringify(chat.getState()));
  fixture.document.querySelector<HTMLElement>('#visibility-host')!.setAttribute('aria-hidden', 'true');
  const hiddenState = chat.getState();
  const sent = await chat.send('must not send after the accepted panel becomes hidden');
  check('hiding an accepted panel without detach revokes readiness and DOM send',
    hiddenState.panelFound === false && hiddenState.composerFound === false
      && chat.getState().panelFound === false && clicks === 0 && input.value === ''
      && sent.confirmed === false && sent.reason === 'composer_not_found',
    `${JSON.stringify(hiddenState)}:${JSON.stringify(chat.getState())}:${clicks}:${input.value}:${JSON.stringify(sent)}`);
  chat.destroy();
  fixture.restore();
}

{
  const fixture = installDom(`
    <button id="observed-open" aria-label="Chat with everyone"></button>
    <main id="meet-app-shell">
      <div role="log" aria-live="polite">
        ${messageHtml('shell-decoy', 'Generic application notification', 'Elsewhere')}
      </div>
      <textarea id="preexisting-decoy" aria-label="Send a message"></textarea>
      <div id="side-panel-mount"></div>
    </main>`);
  const received: Array<{ sender: string; text: string }> = [];
  fixture.document.querySelector<HTMLButtonElement>('#observed-open')!.onclick = () => {
    fixture.document.querySelector('#side-panel-mount')!.insertAdjacentHTML(
      'beforeend', observedUnlabeledPanelHtml(messageHtml('observed-old', 'Histórico', 'Ana')),
    );
  };
  const chat = createGmeetChat({
    botName: 'Marvin', pollMs: 5, historySettleMs: 0, sendTimeoutMs: 80,
    onMessage: (message) => received.push(message),
  });
  await wait(10);
  const observedPanel = fixture.document.querySelector('#observed-chat-surface')!;
  const observedInput = observedPanel.querySelector<HTMLTextAreaElement>('textarea')!;
  observedPanel.querySelector<HTMLButtonElement>('button')!.onclick = () => {
    observedPanel.querySelector('[data-chat-messages]')!.insertAdjacentHTML(
      'beforeend', messageHtml('observed-own', observedInput.value, 'You'),
    );
    observedInput.value = '';
  };
  observedPanel.querySelector('[data-chat-messages]')!.insertAdjacentHTML(
    'beforeend', messageHtml('observed-new', 'Marvin, consegue me ouvir?', 'Bruno'),
  );
  await wait(15);
  const sent = await chat.send('Consigo, sim.');
  check('accepts only the unlabeled chat subtree born from the opener transition',
    chat.getState().panelFound && chat.getState().composerFound
      && fixture.document.querySelector<HTMLTextAreaElement>('#preexisting-decoy')!.value === '',
    JSON.stringify(chat.getState()));
  check('captures from the observed unlabeled Meet panel after priming',
    received.length === 1 && received[0]?.text === 'Marvin, consegue me ouvir?', JSON.stringify(received));
  check('sends and confirms by readback inside the observed unlabeled Meet panel',
    sent.confirmed === true, JSON.stringify(sent));
  chat.destroy();
  fixture.restore();
}

{
  const fixture = installDom(`
    <button id="already-open" aria-label="Chat with everyone" aria-expanded="true"></button>
    <textarea id="already-open-decoy" aria-label="Send a message"></textarea>
    <div id="already-open-mount"></div>`);
  const opener = fixture.document.querySelector<HTMLButtonElement>('#already-open')!;
  const mount = fixture.document.querySelector('#already-open-mount')!;
  const renderPanel = (includeRecoveryMention = false) => mount.insertAdjacentHTML(
    'beforeend', observedUnlabeledPanelHtml(
      [
        messageHtml('already-open-old', 'Histórico', 'Ana'),
        includeRecoveryMention
          ? messageHtml('already-open-during-recovery', 'Marvin, chegou enquanto o painel fechava', 'Carla')
          : '',
      ].join(''),
      'already-open-panel',
    ),
  );
  renderPanel();
  let openerTransitions = 0;
  opener.onclick = () => {
    openerTransitions++;
    if (opener.getAttribute('aria-expanded') === 'true') {
      opener.setAttribute('aria-expanded', 'false');
      fixture.document.querySelector('#already-open-panel')?.remove();
    } else {
      opener.setAttribute('aria-expanded', 'true');
      renderPanel(true);
    }
  };
  const received: Array<{ sender: string; text: string }> = [];
  const chat = createGmeetChat({
    botName: 'Marvin', pollMs: 5, openRetryMs: 10, historySettleMs: 0, sendTimeoutMs: 80,
    onMessage: (message) => received.push(message),
  });
  await wait(20);
  const panel = fixture.document.querySelector('#already-open-panel')!;
  const input = panel.querySelector<HTMLTextAreaElement>('textarea')!;
  panel.querySelector<HTMLButtonElement>('button')!.onclick = () => {
    panel.querySelector('[data-chat-messages]')!.insertAdjacentHTML(
      'beforeend', messageHtml('already-open-own', input.value, 'You'),
    );
    input.value = '';
  };
  panel.querySelector('[data-chat-messages]')!.insertAdjacentHTML(
    'beforeend', messageHtml('already-open-new', 'Marvin, painel já estava aberto', 'Bruno'),
  );
  await wait(15);
  const sent = await chat.send('Painel reconhecido.');
  check('re-authorizes an already-open unlabeled panel through a close and reopen transition',
    openerTransitions === 2 && chat.getState().panelFound && chat.getState().primed
      && chat.getState().composerFound
      && fixture.document.querySelector<HTMLTextAreaElement>('#already-open-decoy')!.value === '',
    `${openerTransitions}:${JSON.stringify(chat.getState())}`);
  check('preserves priming, capture and readback across already-open recovery',
    received.length === 2
      && received[0]?.text === 'Marvin, chegou enquanto o painel fechava'
      && received[1]?.text === 'Marvin, painel já estava aberto' && sent.confirmed === true,
    `${JSON.stringify(received)}:${JSON.stringify(sent)}`);
  chat.destroy();
  fixture.restore();
}

{
  const fixture = installDom(`
    <button aria-label="Chat with everyone" aria-expanded="true"></button>
    <main id="already-open-app-shell">
      <div role="log" aria-live="polite">
        ${messageHtml('already-open-shell-decoy', 'Generic application notification', 'Elsewhere')}
      </div>
      <textarea id="already-open-shell-composer" aria-label="Send a message"></textarea>
    </main>`);
  const chat = createGmeetChat({ botName: 'Marvin', pollMs: 5, historySettleMs: 0, onMessage: () => {} });
  await wait(10);
  const sent = await chat.send('must not reach an already-open app shell');
  check('an open opener never authorizes an app-shell landmark and sibling composer',
    chat.getState().panelFound === false && chat.getState().composerFound === false
      && fixture.document.querySelector<HTMLTextAreaElement>('#already-open-shell-composer')!.value === ''
      && sent.confirmed === false,
    `${JSON.stringify(chat.getState())}:${JSON.stringify(sent)}`);
  chat.destroy();
  fixture.restore();
}

{
  const fixture = installDom(`
    <button id="stuck-open" aria-label="Chat with everyone" aria-pressed="true"></button>
    <div id="generic-open-app-shell">
      ${messageHtml('generic-open-decoy', 'Generic application notification', 'Elsewhere')}
      <textarea aria-label="Send a message"></textarea>
    </div>`);
  let openerTransitions = 0;
  const opener = fixture.document.querySelector<HTMLButtonElement>('#stuck-open')!;
  opener.onclick = () => {
    openerTransitions++;
    opener.setAttribute('aria-pressed', opener.getAttribute('aria-pressed') === 'true' ? 'false' : 'true');
  };
  const chat = createGmeetChat({
    botName: 'Marvin', pollMs: 2, openRetryMs: 5, historySettleMs: 0, onMessage: () => {},
  });
  await wait(25);
  check('an open opener never authorizes a generic DIV application shell without a transition',
    openerTransitions === 4 && chat.getState().panelFound === false && chat.getState().composerFound === false,
    `${openerTransitions}:${JSON.stringify(chat.getState())}`);
  chat.destroy();
  fixture.restore();
}

{
  const fixture = installDom(`
    <button aria-label="Chat with everyone" aria-pressed="true"></button>
    <section id="hidden-open-panel" hidden>
      ${messageHtml('hidden-open-message', 'Hidden history', 'Ana')}
      <textarea aria-label="Send a message"></textarea>
    </section>`);
  const chat = createGmeetChat({ botName: 'Marvin', pollMs: 5, historySettleMs: 0, onMessage: () => {} });
  await wait(10);
  check('an open opener does not authorize a hidden local composer',
    chat.getState().panelFound === false && chat.getState().composerFound === false,
    JSON.stringify(chat.getState()));
  chat.destroy();
  fixture.restore();
}

{
  const fixture = installDom(`
    <button id="empty-open" aria-label="Chat with everyone"></button>
    <textarea id="empty-decoy" aria-label="Send a message"></textarea>
    <div id="empty-mount"></div>`);
  fixture.document.querySelector<HTMLButtonElement>('#empty-open')!.onclick = () => {
    fixture.document.querySelector('#empty-mount')!.insertAdjacentHTML(
      'beforeend', observedUnlabeledPanelHtml('', 'empty-observed-chat-surface'),
    );
  };
  const chat = createGmeetChat({
    botName: 'Marvin', pollMs: 5, historySettleMs: 0, onMessage: () => {},
  });
  await wait(10);
  check('accepts an initially empty unlabeled panel only when its composer is new after the opener',
    chat.getState().panelFound && chat.getState().primed && chat.getState().composerFound
      && fixture.document.querySelector<HTMLTextAreaElement>('#empty-decoy')!.value === '',
    JSON.stringify(chat.getState()));
  chat.destroy();
  fixture.restore();
}

{
  const fixture = installDom(`
    <div role="log" aria-live="polite">${messageHtml('landmark-only', 'History', 'Ana')}</div>
    <textarea aria-label="Send a message"></textarea>`);
  const chat = createGmeetChat({ botName: 'Marvin', pollMs: 5, historySettleMs: 0, onMessage: () => {} });
  await wait(10);
  check('rejects a landmark whose composer is outside the detected panel',
    chat.getState().panelFound === false && chat.getState().composerFound === false);
  chat.destroy();
  fixture.restore();
}

{
  const fixture = installDom(panelHtml(''));
  const chat = createGmeetChat({ botName: 'Marvin', pollMs: 5, historySettleMs: 0, onMessage: () => {} });
  await wait(10);
  check('reports the bridge ready only with panel, primed history and a scoped composer',
    chat.getState().panelFound === true && chat.getState().primed === true
      && chat.getState().composerFound === true);
  chat.destroy();
  fixture.restore();
}

{
  const fixture = installDom(panelHtml(''));
  fixture.document.querySelector<HTMLTextAreaElement>('textarea')!.disabled = true;
  const chat = createGmeetChat({ botName: 'Marvin', pollMs: 5, historySettleMs: 0, onMessage: () => {} });
  await wait(10);
  check('reports a disabled scoped composer as unavailable', chat.getState().composerFound === false);
  chat.destroy();
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
  const fixture = installDom('<button id="untimestamped-open" aria-label="Chat with everyone"></button>');
  fixture.document.querySelector<HTMLElement>('#untimestamped-open')!.onclick = () => {
    fixture.document.body.insertAdjacentHTML('beforeend', panelHtml(''));
    setTimeout(() => fixture.document.querySelector('#messages')!.insertAdjacentHTML(
      'beforeend', messageHtml('old-common-during-prime', 'Aviso histórico comum', 'Ana')), 5);
    setTimeout(() => fixture.document.querySelector('#messages')!.insertAdjacentHTML(
      'beforeend', messageHtml('new-mention-during-prime', 'Marvin, pergunta que acabou de chegar', 'Bruno')), 8);
  };
  const received: Array<{ sender: string; text: string }> = [];
  const chat = createGmeetChat({
    botName: 'Marvin · XMACNA — transcrevendo', pollMs: 5, historySettleMs: 20,
    onMessage: (message) => received.push(message),
  });
  await wait(55);
  check('delivers an untimestamped bot mention that arrives while history is priming exactly once',
    received.length === 1 && received[0]?.text === 'Marvin, pergunta que acabou de chegar',
    JSON.stringify(received));
  check('continues suppressing untimestamped common history rendered during priming',
    !received.some((message) => message.text === 'Aviso histórico comum'), JSON.stringify(received));
  chat.destroy();
  fixture.restore();
}

{
  const fixture = installDom('<button id="concurrent-open" aria-label="Chat with everyone"></button>');
  const now = Date.now();
  fixture.document.querySelector<HTMLElement>('#concurrent-open')!.onclick = () => {
    fixture.document.body.insertAdjacentHTML('beforeend', panelHtml(''));
    setTimeout(() => fixture.document.querySelector('#messages')!.insertAdjacentHTML(
      'beforeend', messageHtml('old-during-prime', 'Marvin, pergunta histórica', 'Ana', new Date(now - 60_000).toISOString())), 5);
    setTimeout(() => fixture.document.querySelector('#messages')!.insertAdjacentHTML(
      'beforeend', messageHtml('new-during-prime', 'Marvin, pergunta que acabou de chegar', 'Bruno', new Date(now + 1_000).toISOString())), 8);
  };
  const received: Array<{ sender: string; text: string }> = [];
  const chat = createGmeetChat({
    botName: 'Marvin', pollMs: 5, historySettleMs: 20,
    onMessage: (message) => received.push(message),
  });
  await wait(55);
  check('delivers a new timestamped message that arrives while history is priming',
    received.length === 1 && received[0]?.text === 'Marvin, pergunta que acabou de chegar',
    JSON.stringify(received));
  check('does not replay timestamped history rendered during priming',
    !received.some((message) => message.text.includes('histórica')), JSON.stringify(received));
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
    <button id="observed-reopen" aria-label="Chat with everyone" aria-pressed="false"></button>
    <div id="observed-reopen-mount"></div>`);
  let reopenClicks = 0;
  fixture.document.querySelector<HTMLButtonElement>('#observed-reopen')!.onclick = () => {
    reopenClicks++;
    const messages = reopenClicks === 1
      ? messageHtml('observed-stable-old', 'Já observada', 'Ana')
      : [
          messageHtml('observed-stable-old', 'Já observada', 'Ana'),
          messageHtml('observed-after-reopen', 'Nova depois da reabertura', 'Ana'),
        ].join('');
    fixture.document.querySelector('#observed-reopen-mount')!.insertAdjacentHTML(
      'beforeend', observedUnlabeledPanelHtml(messages, `observed-reopened-${reopenClicks}`),
    );
  };
  const received: Array<{ sender: string; text: string }> = [];
  const chat = createGmeetChat({
    botName: 'Marvin', pollMs: 5, historySettleMs: 0,
    onMessage: (message) => received.push(message),
  });
  await wait(10);
  fixture.document.querySelector('#observed-reopened-1')!.remove();
  await wait(25);
  check('re-authorizes an unlabeled panel from a fresh opener transition after cached detach',
    reopenClicks === 2 && chat.getState().panelFound && chat.getState().composerFound,
    `${reopenClicks}:${JSON.stringify(chat.getState())}`);
  check('dedupes stable ids while capturing from the re-authorized unlabeled panel',
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
