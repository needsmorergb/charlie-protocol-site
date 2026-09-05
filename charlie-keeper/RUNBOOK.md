# Charlie keeper -- runbook

This folder is a self-contained copy of the Charlie Protocol BURN leg, run
from your own wallet: every crank buys $CHARLIE on its PumpSwap pool and
burns the tokens in the same transaction. It needs Python 3.11 or newer and
nothing else -- no packages, no build step. The whole thing is standard
library, on purpose, so it runs the same on a Windows box, a Mac or a VPS.

Nothing in this folder signs anything until `keeper.json` says
`"armed": true`. Everything below up to step 6 can be done unarmed.

## 1. Make a dedicated keeper wallet

Do not use your main wallet. Make a fresh keypair and fund it with only
what you intend the keeper to spend, plus about 0.02 SOL for fees and rent.

With the Solana CLI:

```
solana-keygen new --outfile keeper-keypair.json --no-bip39-passphrase
solana address -k keeper-keypair.json
```

Or export a fresh wallet's secret key from Phantom/Solflare and save the
base58 string as the only line of `keeper-keypair.json`. Both forms are
accepted. Keep the file in this folder and readable only by you.

If you want the keeper to also retire tokens you already hold, send those
tokens to the keeper wallet; `also_burn_tokens` burns that many per crank
from the keeper wallet's own balance.

## 2. Write the config

```
python keeper.py init
```

writes `keeper.json`. Fill in:

| key | what it is |
|---|---|
| `wallet` | the keeper wallet's address. The keypair file must be this wallet or the keeper refuses to start |
| `keypair` | path to the key file (relative to this folder is fine) |
| `rpc` | one or more RPC URLs. The defaults are public nodes; a paid endpoint is steadier |
| `lot_sol` | SOL per crank. 0.05 is the protocol's figure. Above 1 SOL needs `"allow_large_lot": true` |
| `every_seconds` | seconds between cranks. Minimum 60; 3600 is one an hour |
| `max_total_sol` | the budget. The keeper stops when it is reached and never runs uncapped |
| `also_burn_tokens` | held tokens to burn alongside each buy, 0 for none |
| `slippage_bps` | how far below the quote the buy may deliver before it fails whole (100 = 1%) |
| `priority_fee_micro_lamports` | 0 unless the network is congested |
| `armed` | leave `false` until step 6 |
| `notify_url` | optional webhook (Slack/Discord-style) told about every crank and stop |

Leave `mint` as $CHARLIE's address unless you mean to run this for another coin.

## 3. Preflight

```
python keeper.py preflight
```

or `.\Start-CharlieKeeper.ps1 -Preflight` on Windows. Every row is one
thing that could be wrong -- Python version, keypair matches the named
wallet, RPC answers, the coin and its pool read correctly, the wallet holds
enough, one crank simulates clean against mainnet -- with the check that
decided it. The last line says GO or NO-GO. Unarmed, it shows one WARN row
for that; everything else must be PASS.

Do not proceed past a FAIL. The detail column says what to fix.

## 4. Read what one crank would do

The preflight's `simulation` row shows the tokens one lot buys, the price
move it causes and the compute it uses, quoted against the live pool.
For the full plan with every figure and its source:

```
python -m indexer buyback 8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump --wallet <keeper wallet>
```

## 5. Decide the budget and cadence

The price move of one crank is roughly the lot divided by the pool's SOL
reserve; the preflight prints it. What a keeper produces over weeks is
steady buy volume, a falling supply, creator fees flowing into $CHARLIE's
own SOL burn, and a public record on the coin's page. It does not produce a
price floor, and nothing here will claim it does. Set `max_total_sol` to a
number you are content to have spent.

## 6. Arm and run one crank

Set `"armed": true`, then:

```
python keeper.py once
```

It builds, simulates, signs, sends, waits for confirmation, then reads the
transaction back through the indexer's own decoders and prints one JSON
line: the signature, the tokens burned, `atomic PASS`. Open the signature
on solscan.io and look at it. The state file now shows one crank and the
SOL it used against the budget.

## 7. Run the keeper

Foreground:

```
python keeper.py run
```

Windows, unattended: `.\Register-CharlieKeeperTask.ps1` registers a
Scheduled Task that runs `Start-CharlieKeeper.ps1` at logon and re-checks
hourly. `Start-CharlieKeeper.ps1` restarts the keeper if it crashes, and
the state file carries the budget across restarts, so being restarted can
never make it spend more than `max_total_sol`.

Elsewhere, any supervisor that runs `python keeper.py run` and restarts it
on a non-zero exit does the same job. Exit codes: 0 stopped for a stated
reason, 1 stopped on repeated failures, 2 config refused, 3 not armed,
4 budget already spent.

## 8. Watch it

```
python keeper.py status          # cranks, spend against budget, last signature, last log lines
python keeper.py status --live   # plus the wallet, the pool and the supply right now
```

`keeper.log.jsonl` holds one line per crank attempt. After cranks have
landed, the coin's page at charlieprotocol.fun records them the next time
its burn walk runs; on the site repository that is
`python -m indexer scan <mint> --evidence state/evidence.db`.

## 9. Stop it

Create the stop file (default `keeper.stop` in this folder):

```
New-Item keeper.stop        # PowerShell
touch keeper.stop           # elsewhere
```

The keeper stops before its next crank and says so. Delete the file before
starting again. To stop for good on Windows, also run
`.\Register-CharlieKeeperTask.ps1 -Remove`.

Raising `max_total_sol` in the config extends a finished budget. Deleting
`keeper-state.json` resets the count to zero; do that only if you mean to
start the budget over.

## What the record will say

Each crank is a burn against the mint by a third party: source `spl_burn`,
`atomic PASS` (the swap shares the transaction), `protocol_attributed 0`
(no protocol program cranked it). It counts toward supply destroyed and the
page's "burned by hand" figure. It is not a protocol burn and the page will
not call it one.

## If something goes wrong

* **`simulation FAIL: the pool moved`** -- transient; the next crank quotes
  again. If it repeats every time, widen `slippage_bps` a little.
* **`keypair ... REFUSING`** -- the key file is not the wallet the config
  names. Fix whichever is wrong; never "just" edit the wallet to match a
  key you did not mean to use.
* **`rpc FAIL`** -- every endpoint refused. Add a different provider to
  `rpc`.
* **`5 consecutive failures`** -- the keeper stopped itself. Read the last
  log lines and run preflight before restarting.
* **A crank shows `sent` but `confirm` timed out** -- check the signature
  on an explorer before anything else. If it landed, the state file missed
  it: the budget is now slightly under-counted by one lot; adjust
  `max_total_sol` down by that lot if it matters.

`MANIFEST.json` lists every file in this folder with its hash and the
commit of `charlie-protocol-site` it was built from. `python
scripts/build_keeper.py --check` in that repository confirms this copy is
current.
