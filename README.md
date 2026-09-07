# charlie-protocol-site

The deployed Charlie Protocol site. Vercel serves `web/` and runs the Python handlers in `api/`. Verification uses the same publisher and renderer as the indexer; failed reads are not coin verdicts.

This repository contains two kinds of pages:

- Authored marketing pages: `index.html`, `verify.html`, `coin.html`, `coins.html`, `enroll.html`, `404.html`, and their shared assets. Their `charlie:authored-page` marker protects them from indexer page generation. Edit these here; keep any displayed observations traceable to committed JSON and label their reading time.
- Generated evidence pages: `<mint>.html`, `<mint>.json`, and `coins-<page>.html`. Update the generator and regenerate these. Machine-readable statuses and publication gates must remain intact. Human-facing status wording uses “Needs review” for a failed check; it never converts that check into a pass.

`/coin/<mint>.json` and `/verify/<mint>` use `api/verify.py`. Bundled observations take precedence, and responses may be cached at the edge for 60 seconds. Other addresses are read through the RPC gateway. The verification form sends requests only on submit or preset selection.

The shared Python code also lives in [charlie-protocol-v1](https://github.com/needsmorergb/charlie-protocol-v1). `.github/workflows/sync.yml` checks parity. Changes to `indexer/publish.py` and `indexer/site.py` here must also be carried to that source repository before release; do not weaken the sync check.

Local verification:

```sh
python3 -m unittest discover -s tests
node --test tests/verify-client.test.cjs
node --check web/assets/charlie-motion.js
node --check web/assets/signature-motion.js
```
