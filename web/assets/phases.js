/* The phases section grades itself from a committed observation.
 *
 * WHY THIS FETCHES RATHER THAN SHIPPING HARDCODED STATES. index.html carries
 * the `charlie:authored-page` marker, so `site.write_landing()` skips it and
 * no publishing job ever regenerates this page. A hand-written verdict table
 * here would freeze on the day it was written and then quietly lie -- the
 * precise failure this section exists to describe. The coin record IS
 * regenerated (intake.yml runs `indexer site --write --landing` and commits
 * web/), so reading the gate states out of it at runtime keeps this honest
 * for free, and the cards re-grade themselves when the evidence walk runs.
 *
 * WHAT IT REFUSES TO DO. It never invents a verdict. If the record cannot be
 * read, every gate stays UNCHECKED and the section says so in those words,
 * rather than rendering blanks a reader could mistake for findings -- the
 * same refusal verify.js makes on this page.
 */
(() => {
  'use strict';

  var RECORD = '/coin/8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump.json';

  // Mirrors indexer/phases_page.py::_PHASES. A phase with gates carries NO
  // hand-written status: it is graded, or it reads OPEN. A phase without
  // gates carries `stated`, because no check was ever going to decide it.
  var PHASES = [
    {
      n: 1,
      title: '$CHARLIE, and the watcher that proved the thesis on it',
      landed: '2026-08-23',
      body: 'Before there was a protocol there was one coin and one question: can a fee stream be routed somewhere no key can spend, and can a stranger confirm it. Its sharing config is admin_revoked with a single shareholder, so the split is permanent. Mint and freeze authority are both revoked, so nothing it destroys can be reissued.',
      gates: ['SOL_BURN_UNSPENDABLE', 'BURN_IRREVERSIBLE']
    },
    {
      n: 2,
      title: 'The spec, and an indexer that reads any coin',
      landed: '2026-08-29',
      body: 'The specification was published on the day the code that implements it did not exist. The indexer reads a pump sharing config, attributes every shareholder to a leg, and consults no allowlist and no consent table -- so it reads any pump coin whether or not that coin has heard of this protocol.',
      gates: ['CONFIG_MINT', 'SPLIT_SUM']
    },
    {
      n: 3,
      title: 'The evidence walk, the public surface, and the crank',
      landed: '2026-09-05',
      body: 'Fee inflows reconciled per destination, SPL burn and boost decoders, and initial supply derived from the coin’s own CreateEvent. Then the surface that publishes it, and the crank that pays every enrolled coin hourly through pump’s own permissionless instruction.',
      gates: ['SOL_BURN_BALANCE', 'BURN_SUPPLY', 'OPS_ROUTED', 'PROTOCOL_SHARE']
    },
    {
      n: 4,
      title: 'The program',
      landed: '2026-09-12',
      body: 'Four instructions: init_charlie_pool, init_route, set_route, distribute. The guarantee is the absence of code. Nothing debits a coin’s collector except distribute, and no instruction anywhere sends to an address its caller supplied.',
      gates: [],
      stated: 'DEVNET',
      code: 'GFA3nG9geMhpPXaExVLGYBtj6aJX7S125dLzv4EcXGiG',
      codeLabel: 'devnet program id',
      note: 'No check gates this phase, and it is the phase where saying so matters most: a program existing is not a check passing. It is deployed to devnet and is not deployed to mainnet.'
    },
    {
      n: 5,
      title: 'Mainnet deploy, and revoking upgrade authority',
      landed: null,
      target: '2026-09-25',
      body: 'The absence-of-code guarantee only means anything once the program is immutable, and revoking upgrade authority is a one-way door that freezes every bug permanently. So the order is: deploy upgradeable, run the whole pipeline in production against one live coin, then revoke.',
      gates: ['BURN_SPEND', 'BURN_ATOMIC'],
      stated: 'GATED',
      note: 'Blocked on funding. The gate is <strong>0.943958 SOL</strong> net and <strong>1.415504 SOL</strong> in the wallet on the day, the difference being a buffer account the loader reclaims once the deploy lands.'
    }
  ];

  var STATE_GLYPH = {SHIPPED: '✓', OPEN: '○', DEVNET: '◐', GATED: '▣', SLIPPED: '△'};
  var STATE_DESC = {
    SHIPPED: 'Checks ran and returned a verdict.',
    OPEN: 'Checks not run yet. Nothing has failed.',
    DEVNET: 'Built and deployed to devnet. Not deployed to mainnet.',
    GATED: 'Blocked on funding. The work is specified and the checks are written.',
    SLIPPED: 'Target date passed with the criterion unmet.'
  };

  function esc(value) {
    return String(value).replace(/[&<>"]/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c];
    });
  }

  // A phase is done when every check gating it returns a verdict. FAIL counts:
  // a check that failed has measured something, which is what "done" means
  // here. It does not mean the coin is healthy -- the coin page is where that
  // is read.
  function gradeOf(phase, byName) {
    if (!phase.gates.length) return phase.stated || 'OPEN';
    var settled = phase.gates.every(function (name) {
      var status = byName[name];
      return status && status !== 'UNCHECKED';
    });
    if (settled) return 'SHIPPED';
    if (phase.stated === 'GATED') return 'GATED';
    if (phase.target && new Date() > new Date(phase.target + 'T00:00:00Z')) return 'SLIPPED';
    return 'OPEN';
  }

  function buildAxis(phase) {
    if (phase.landed) return {glyph: '●', text: 'shipped ' + phase.landed};
    if (phase.target) return {glyph: '○', text: 'target ' + phase.target};
    return {glyph: '○', text: 'not built'};
  }

  function renderCard(phase, byName, index) {
    var state = gradeOf(phase, byName);
    var axis = buildAxis(phase);

    return '<li class="phase-card cyber-card cyber-card--chamfer phase-reveal phase-card--' +
      state.toLowerCase() + '" style="--i:' + index + '">' +
      '<span class="cyber-corner cyber-corner--tl"></span>' +
      '<span class="cyber-corner cyber-corner--br"></span>' +
      '<div class="phase-head">' +
        '<span class="phase-ordinal">Phase ' + phase.n + '</span>' +
        '<span class="badge badge-' + (state === 'SHIPPED' ? 'pass' : 'unchecked') + '">' +
          '<span aria-hidden="true">' + STATE_GLYPH[state] + '</span> ' + state +
        '</span>' +
        '<h3>' + esc(phase.title) + '</h3>' +
      '</div>' +
      '<p class="sr-only">Status: ' + state + '. ' + STATE_DESC[state] + '</p>' +
      '<div class="phase-axes">' +
        '<div class="phase-axis"><span class="phase-axis-label">Build</span>' +
          '<span class="phase-axis-value"><span class="phase-glyph" aria-hidden="true">' +
          axis.glyph + '</span>' + esc(axis.text) + '</span></div>' +
      '</div>' +
      '<p class="phase-body">' + esc(phase.body) + '</p>' +
      (phase.note ? '<p class="phase-note">' + phase.note + '</p>' : '') +
      (phase.code ? '<code class="phase-code">' + esc(phase.code) + '</code>' +
        '<span class="phase-code-label">' + esc(phase.codeLabel) + '</span>' : '') +
      '</li>';
  }

  function paint(byName, read) {
    var mount = document.getElementById('phases-mount');
    if (!mount) return;
    mount.innerHTML =
      '<ol class="phase-list">' +
      PHASES.map(function (p, i) { return renderCard(p, byName, i); }).join('') +
      '</ol>';

    // Arrival only. Deliberately NOT a state animation: a badge that animated
    // into SHIPPED would dramatise a verdict no check returned.
    var reduced = matchMedia('(prefers-reduced-motion: reduce)');
    if (reduced.matches || !('IntersectionObserver' in window)) {
      // Cards are visible by default. Nothing to do: never hide them behind a
      // class that this path is not going to add.
      return;
    }
    // Only now does the hidden-then-revealed contract switch on. Setting this
    // before observing is the whole safeguard -- if anything below throws, the
    // cards are already painted rather than waiting on a class.
    mount.classList.add('motion-ready');
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        entry.target.classList.add('revealed');
        io.unobserve(entry.target);
      });
    }, {threshold: 0.12});
    mount.querySelectorAll('.phase-reveal').forEach(function (el) { io.observe(el); });
    // content-visibility: auto lets the browser skip layout for offscreen
    // cards, and an observer cannot report what was never laid out. A card
    // still unrevealed after the reveal window is shown unconditionally.
    setTimeout(function () {
      mount.querySelectorAll('.phase-reveal:not(.revealed)').forEach(function (el) {
        el.classList.add('revealed');
      });
    }, 1200);

  }

  function start() {
    // Every gate UNCHECKED until a record says otherwise. An unreadable
    // record must never render as a verdict.
    var byName = {};
    fetch(RECORD, {cache: 'no-cache'})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (record) {
        var read = 'no observation';
        if (record && Array.isArray(record.checks)) {
          record.checks.forEach(function (c) { byName[c.name] = c.status; });
          if (record.observed_at) {
            read = new Date(record.observed_at * 1000).toISOString().slice(0, 10);
          }
        }
        paint(byName, read);
      })
      .catch(function () { paint({}, 'no observation'); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, {once: true});
  } else {
    start();
  }
})();
