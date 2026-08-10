# Claude instructions

Read `AGENTS.md` in this folder and follow it **in order** — it is the
authoritative playbook for installing, setting up, and running this project.

Key rules: you do every automatable step yourself and only ask the user for what
software cannot do (accounts/API keys). Installation is the manual recipe in
`AGENTS.md` (create the venv, `pip install -e` both packages, `npm install`,
write `.env`, log in the Claude subscription, `bionico start`) — there is no
`bionico install`/`provision`/`doctor` command. The only `bionico` commands are
orchestrator control: `skills`, `start`, `stop`, `restart`, `status`,
`autostart`.
