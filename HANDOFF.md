# Handoff

The full handoff lives in the source repository:
https://github.com/needsmorergb/charlie-protocol-v1/blob/main/HANDOFF.md

This repository is the deploy. Vercel serves `web/` and the functions in
`api/`; the workflows run the indexer against mainnet. The shared files
(`indexer/*`, `api/*`, `vercel.json`, `web/assets`) are copied here from
charlie-protocol-v1 by `tools/shared_sync.py`; edit them there, never here.

What only this repository holds, as of 2026-09-05:

- **Workflows.** `intake.yml` (every six hours: load the evidence export,
  measure the queue, regenerate pages, export back, commit).
  `distribute.yml` (hourly: the crank that pays every enrolled coin's
  shareholders; simulates only until the secret below exists).
  `enroll.yml` (on push: simulate the exact enrollment transaction and the
  crank against mainnet; on dispatch: also ask production the questions a
  browser asks). `sync.yml` fails on drift from the source repository.
- **The one open action here: add the repository secret
  `CHARLIE_CRANK_KEYPAIR`**, the JSON key array of a separate low-value
  wallet holding about 0.05 SOL for network fees. Until then the crank
  sends nothing. The owner makes that key; a session never should.
- **Committed evidence** is the text export under `state/evidence/`, never
  the binary db. Committed pages under `web/` must equal what the current
  renderer produces; a merge conflict on one is resolved by taking the
  newer renderer's output, and the next intake regenerates everything.
- **The RPC gateway** (`CHARLIE_RPC_URLS` in each workflow) answers 429
  after several chain-reading runs in quick succession. Space out manual
  dispatches. After a merge, wait for the Vercel production deployment to
  be READY before dispatching `enroll.yml`, or its page check races the
  deploy.
