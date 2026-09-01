# charlie-protocol-site

The deployed build of **charlieprotocol.fun**. This repository holds output
only — no source. Vercel builds it with no build step: `vercel.json` sets
`outputDirectory` to `web/`, and the two `/coin/...` routes are rewrites onto
the flat files in that directory.

Nothing here is hand-written. Every file under `web/` is produced by the
`site` subcommand of the indexer, which lives in the source repository:

    https://github.com/needsmorergb/charlie-protocol-v1

    python -m indexer site 8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump \
        --evidence state/evidence.db --write --out web --landing

Do not edit these files here. A hand edit makes the deployed page and the
generator that produced it disagree, which is exactly the failure this
project exists to make visible. Regenerate in the source repository and copy
the result across.

The pages state their own limits, including the one this repository cannot
fix: nothing independently checks that the renderer faithfully reflects the
record it was generated from.
