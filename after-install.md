# novita-sandbox is installed

## Next step

```bash
hermes novita-sandbox setup
```

That is the whole setup. It will:

1. install the `novita-sandbox` SDK if it is missing,
2. ask for your API key (masked prompt) and validate it against the live API,
3. store the key in `~/.hermes/.env` — never in `config.yaml`,
4. let you pick a sandbox template,
5. set `terminal.backend: novita`,
6. run a real command through the backend to prove it works.

Get an API key at <https://novita.ai/dashboard> (Team tab).

## Afterwards

```bash
hermes novita-sandbox doctor               # is everything wired up?
hermes novita-sandbox doctor --sandboxes   # sandboxes on your account
hermes novita-sandbox stop --all           # stop billing (pauses every sandbox)
hermes novita-sandbox patch-setup          # add a Novita row to `hermes setup`
hermes config set terminal.backend local   # revert to running locally
```

## One thing worth knowing about cost

A **running** sandbox is billed per second for CPU and memory; a **paused** one
is not. The backend pauses after about 5 idle minutes and resumes automatically
on the next command, so idle time is free — but if you ever see a sandbox still
marked *Running* in the Novita console, that one is being billed. Check with
`doctor --sandboxes` and stop it with `stop --all`.

Full detail, including the exact rates, is in `README.md` under **Cost**.
