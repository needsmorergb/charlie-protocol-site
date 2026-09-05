# Running the BURN leg on $CHARLIE without being its dev

**The short version.** Charlie Protocol's BURN leg is `SOL -> buy the token ->
SPL burn`, atomically, in one transaction (PROTOCOL.md sec.1 and sec.4). The
protocol program that would crank it from a coin's creator fees does not
exist yet, and $CHARLIE's fee split is `admin_revoked` -- nobody can point its
fees anywhere, its deployer included. But nothing in the leg's definition
says whose SOL it has to be. A holder can run the exact same leg from their
own wallet, and the indexer already counts what they burn. That is what
`python -m indexer buyback` does.

## What it does, per crank

One transaction, eight instructions, built byte for byte from pump's
published PumpSwap IDL and SDK (`@pump-fun/pump-swap-sdk` 1.19.0), simulated
against mainnet before anything is signed:

1. compute budget (and an optional priority fee)
2. create your $CHARLIE token account if missing (idempotent)
3. create your wrapped-SOL account if missing (idempotent)
4. move the lot (default 0.05 SOL) into it and sync
5. PumpSwap `buy`: exactly `base_amount_out` tokens for at most the lot
6. SPL `burn` of those tokens (plus any held tokens you add with `--also-burn`)
7. close the wrapped-SOL account, returning whatever the buy did not spend

Both legs land or neither does. If the pool moves so the buy would cost
more than the lot, the whole transaction fails; nothing is overpaid and
nothing is left sitting in a token account.

### What the coin's page will show

The mint-wide burn walk records every burn against the mint, by anyone
(D-09). Each crank becomes a `burn_event` row:

| column | value | why |
|---|---|---|
| `source` | `spl_burn` | not pump's boost |
| `atomic` | `PASS` | a PumpSwap invocation shares the transaction |
| `protocol_attributed` | `0` | no protocol program cranked it (D-10) |

It counts toward *supply destroyed* and toward the page's "burned by hand,
not by boost" figure. It is **not** a protocol burn and the page will not
call it one; `BURN_ATOMIC`, narrowed to protocol burns (D-14), stays
not-applicable. After a crank, `python -m indexer scan <mint> --evidence
state/evidence.db` walks it into the record like any other burn.

### The loop, closed by hand

Every buy pays $CHARLIE's creator fee (0.30%-0.95% of the buy, by market-cap
tier) into the coin's creator vault, and $CHARLIE's split routes 100% of
that to its SOL burn address. So each crank does three things: it is buy
volume, it destroys token supply, and it feeds the SOL burn. That is the
loop the landing page describes, with a wallet standing in for the program.

## Using it

Python 3.11, standard library only. From the repository root:

```bash
# 1. dry run: read the pool, quote one lot, build, simulate. Signs nothing.
python -m indexer buyback 8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump --wallet <your address>

# 2. one real crank, from a Solana CLI keypair file
python -m indexer buyback 8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump --keypair keeper.json --send

# 3. the keeper: 0.05 SOL every hour until 2 SOL is committed,
#    burning 100,000 held tokens alongside each buy
python -m indexer buyback 8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump \
    --keypair keeper.json --send --every 3600 --max-total 2 --also-burn 100000

# burn held tokens outright (no swap, no price effect)
python -m indexer burn 8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump --keypair keeper.json --amount 1000000 --send
```

`scripts/Start-CharlieBuyback.ps1` wraps the same command for PowerShell,
with the same switches, logging each crank to a file.

Set `CHARLIE_RPC_URLS` (or `--rpc`) to the endpoint you want; the defaults
are public nodes. Without `--send` nothing is ever signed, and the unsigned
transaction is printed for a browser wallet. With `--send`, a simulation that
reports an error stops the run before the key is used. A crank that lands is
read back through the indexer's own decoders and the result is printed:
tokens burned, swap present, atomic verdict.

**Use a dedicated keypair holding only what you mean to spend.** The file is
read once per crank and never printed or transmitted.

## Price action, honestly

A constant-product pool moves price by the buy's share of the quote reserve:
a lot of `q` SOL into a pool holding `Q` SOL moves price by roughly
`q / Q`. The dry run prints the exact figure (`price after`, `impact_bps`)
against the live reserves. Three things follow, and the tool says them
rather than leaving them to be discovered:

* **A single 0.05 SOL crank barely moves the price** on any pool with real
  depth -- by design (ARCHITECTURE.md sec.2: fixed lots that are never worth
  attacking). What a keeper produces is steady buy volume, a falling supply
  and a public, verifiable record, not a candle.
* **Burning your 16M held tokens reduces supply by about 1.7%.** It is a
  commitment device, and the page records it. It is not a buy and it moves
  no price on its own. `--also-burn` lets you retire it in tranches
  alongside real buys so each crank carries both.
* **Whatever moves the price is the SOL you put in**, and the budget cap is
  yours to set. A protocol that promised otherwise would be making the
  claim PROTOCOL.md sec.2 forbids ("does not create a price floor").

## What this is not

* Not enrollment. $CHARLIE's split cannot be changed; this changes nothing
  about it.
* Not a protocol crank. The SOL comes from a wallet, not a PDA: this is
  PROTOCOL.md sec.5's option 3, an operator keeper, and is labelled as such.
* Not tested on mainnet from this repository's build environment, which has
  no chain access. The derivations are pinned against pump's published
  mainnet examples and the byte layouts against pump's IDL and SDK; the
  first real run should be a dry run, and the simulation gate exists for
  exactly this.

## Where the code lives

`indexer/buyback.py` (the leg), `indexer/message.py` (legacy message
compilation, byte-identical to `enroll.message`), `indexer/ed25519.py`
(RFC 8032 signing, checked against the RFC's vectors and OpenSSL), and
`tests/test_buyback.py`, `tests/test_message.py`, `tests/test_ed25519.py`.
The source of truth for this project is `needsmorergb/charlie-protocol-v1`;
these modules belong there and should be carried across on the next publish.

## The production copy: `charlie-keeper/`

`charlie-keeper/` is the version to put on the machine that will actually
run this for $CHARLIE. It is a build output, not a hand copy:
`python scripts/build_keeper.py` assembles it from `indexer/` and
`scripts/keeper_template/`, and `MANIFEST.json` records every file's hash
and the commit it came from. `python scripts/build_keeper.py --check` (run
by the test suite) fails if the committed folder has drifted from its source.

Copy the folder anywhere with Python 3.11+ and follow `RUNBOOK.md` inside
it. What it adds over the bare `buyback` command:

| | |
|---|---|
| `keeper.json` | one config: coin, wallet, keypair, lot, cadence, budget, notifications |
| `"armed": false` | the default. Nothing signs until you flip it; preflight, status and dry runs all work unarmed |
| named wallet | the keypair file must be the wallet the config names, or the keeper refuses to start |
| `keeper preflight` | a go/no-go table: Python, key, RPC, coin, pool, balance, one simulated crank |
| `keeper-state.json` | spend and crank count persist, so a restart resumes against the same budget and can never exceed it |
| `keeper.stop` | create the file and the loop stops before its next crank |
| `Start-CharlieKeeper.ps1` | runs the loop and restarts it if it dies |
| `Register-CharlieKeeperTask.ps1` | Windows Scheduled Task: at logon and hourly, so a reboot never leaves it down |

```
python keeper.py init         # write keeper.json, fill in wallet and keypair
python keeper.py preflight    # read every row; GO or NO-GO
python keeper.py once         # after "armed": true -- one crank, verified and logged
python keeper.py run          # the loop, until the budget is spent or keeper.stop appears
python keeper.py status       # what it has done; --live adds the wallet, pool and supply
```
