# Rules of AI Engagement

For an AI agent following the [tunnel-puck README](README.md). Read these
before running anything.

- **Ask before touching the gateway.** `gateway add`, `gateway remove`, `link`, `all`,
  `client`, `disable`, `enable`, `undo`, and `exit` with `TPK_ROUTING=wgt`
  change it (`status` only reads it), and `test`
  with `wgt` brings its jump up for a moment. Other people's traffic may run
  through it. Get the person's go-ahead for that server first, read-only
  commands included, and suggest a test server. `tpk-ctl.sh` also asks on
  the terminal; once the person has agreed for this gateway, `TPK_YES=1` answers
  for you. Reaching the puck through the gateway changes nothing there, but tell the
  person it connects. Follow any rules the person has about which servers you
  may use.
- **Some steps need a person:** everything marked **[Browser]** or
  **[Hands]**, the browser login on the first `ssh SSH_CLOUDFLARE`, making
  client configs with `TPK_ROUTING=tpk`, filling in a `tpk` gateway's settings
  (`gateway add` stops for it), and typing a Wi-Fi password. Stop, say exactly
  what to do, and wait.
- **Name the puck** on every command (`scripts/tpk-ctl.sh home all`) once
  there's more than one, and the gateway too (`home.dengar`) once a puck serves
  more than one: with no terminal, `tpk-ctl.sh` can't ask.
- **Dry run first:** `TPK_DRY_RUN=1 scripts/tpk-ctl.sh all`.
- **Secrets stay put.** Never print a `*.key` file, a `PrivateKey` line, a jump
  file, the Cloudflare token or a Wi-Fi password. The scripts never do.
- **A FAIL means stop.** Report the command, its output and the matching
  [Troubleshooting](TROUBLESHOOTING.md) row. Don't improvise a fix on the gateway.
