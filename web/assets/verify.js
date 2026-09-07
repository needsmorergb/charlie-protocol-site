/* The server owns coin reports. This form only validates and classifies reads. */
(function () {
  'use strict';
  const alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
  function validMint(value) {
    if (!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(value)) return false;
    let number = 0n;
    for (const char of value) number = number * 58n + BigInt(alphabet.indexOf(char));
    const bytes = [];
    while (number > 0n) { bytes.unshift(Number(number % 256n)); number /= 256n; }
    const zeros = value.match(/^1*/)[0].length;
    if (zeros + bytes.length !== 32) return false;
    let reconstructed = 0n;
    for (const byte of bytes) reconstructed = reconstructed * 256n + BigInt(byte);
    let encoded = '';
    while (reconstructed > 0n) {
      encoded = alphabet[Number(reconstructed % 58n)] + encoded;
      reconstructed /= 58n;
    }
    return '1'.repeat(zeros) + encoded === value;
  }
  const form = document.querySelector('.verify-form');
  const input = document.getElementById('verifyInput');
  const panel = document.getElementById('verifyStatus');
  const title = document.getElementById('verifyStatusTitle');
  const body = document.getElementById('verifyStatusBody');
  const states = {
    invalid: ["That doesn't look like a coin address", 'A Solana coin address is 32 to 44 characters long and never uses the digit 0, a capital O, a capital I, or a lowercase l. Check for a missing character or an extra space, then paste it again. It must also decode to a 32-byte address.'],
    unavailable: ["We couldn't read the blockchain just now", 'This is a problem with the computer we ask about Solana — not with this coin. Nothing here passed a check and nothing here failed one, so we are showing you no numbers at all rather than blanks you might mistake for findings. Try again in a moment.'],
    limited: ["You've checked a lot of coins very quickly", 'Reading the blockchain costs us a request every time, so there is a limit on how fast we can do it. Nothing is wrong with this coin and nothing is wrong with your address. Wait about a minute and check it again.'],
    timeout: ['The blockchain took too long to answer', 'We waited 20 seconds and never got an answer back. We stopped waiting rather than guess. This says nothing about the coin — we simply never got to look at it. Try again in a moment.'],
    noSplit: ["This coin doesn't split its trading fees", 'We read the blockchain and it answered. This coin sends its creator fees straight to one ordinary wallet, so there is no split to check. That is a fact about the coin, not a problem on our end. Most coins work this way.']
  };
  function show(heading, message, busy = false) {
    title.textContent = heading;
    body.textContent = message;
    panel.setAttribute('aria-busy', String(busy));
  }
  let active;
  let retryAt = 0;
  async function submit(event) {
    if (event) event.preventDefault();
    if (active) { active.controller.abort(); clearTimeout(active.timer); active = null; }
    const mint = input.value.trim();
    input.value = mint;
    input.setAttribute('aria-invalid', String(!validMint(mint)));
    if (!validMint(mint)) { show(...states.invalid); input.focus(); return; }
    if (Date.now() < retryAt) { show(...states.limited); return; }
    const request = {controller: new AbortController(), timedOut: false};
    active = request;
    request.timer = setTimeout(() => {
      request.timedOut = true;
      request.controller.abort();
    }, 20000);
    show('Request sent', 'Waiting for a saved observation or a new blockchain read. No result is available yet.', true);
    try {
      const response = await fetch('/coin/' + encodeURIComponent(mint) + '.json', {signal: request.controller.signal});
      if (active !== request) return;
      if (response.status === 429) {
        retryAt = Date.now() + 60000;
        show(...states.limited); return;
      }
      if (!response.ok) { show(...states.unavailable); return; }
      const record = await response.json();
      if (active !== request) return;
      if (!record || record.mint !== mint || !Number.isFinite(record.observed_at)) {
        show(...states.unavailable); return;
      }
      if (record.error) {
        if (record.error_kind !== 'no_sharing_config') { show(...states.unavailable); return; }
        show(...states.noSplit);
      } else {
        if (!Array.isArray(record.checks) || !record.checks.length) { show(...states.unavailable); return; }
        show('Observation received', 'Opening the report. It shows the reading time and explains each available figure.', true);
      }
      window.location.assign('/verify/' + encodeURIComponent(mint));
    } catch (error) {
      if (active !== request) return;
      show(...states[request.timedOut ? 'timeout' : 'unavailable']);
    } finally {
      clearTimeout(request.timer);
      if (active === request) { active = null; panel.setAttribute('aria-busy', 'false'); }
    }
  }
  form.addEventListener('submit', submit);
  input.addEventListener('input', () => {
    input.setAttribute('aria-invalid', String(input.value.length > 0 && !validMint(input.value.trim())));
  });
  window.setPresetCA = mint => { input.value = mint; submit(); };
  const mint = new URLSearchParams(window.location.search).get('mint');
  if (mint) input.value = mint; // A query fills the form; only an explicit submit reads the chain.
})();
