# Seat setup

The seats ran on the Codex app-server runtime of BAND Desktop's `band` CLI, one container per seat via `setup/codex-factory` (rootless Podman, project directory mounted read-write, `mandates/` read-only). Model `gpt-5.6-terra`, reasoning effort `high`, service tier `fast`, sandbox `danger-full-access` and approval `never` inside the container.

Create each seat (repeat for `foreman`, `builder`, `assayer`):

```sh
band agent create --session foreman --name foreman --transport codex-app-server \
  --spawn-command "$HOME/.local/bin/codex-factory" --cwd "$PROJECT" \
  --instructions-file "$PROJECT/mandates/foreman.md"
```

Create a room with the three seats and the human, then set each seat's room session (all flags in one call, because a partial call clears the flags left out):

```sh
band chat new --as <owner>/foreman --with <owner>/builder --with <owner>/assayer
band runtime settings --as <owner>/foreman --host-session default-<room-id> \
  --model gpt-5.6-terra --effort high --sandbox danger-full-access --approval never
```

The human then posts the contents of `tasks/stage-1.md` in the room, mentioning the foreman.

`setup/codex-factory` is the launcher used for the run, with home and project paths replaced by `$HOME` and `$PROJECT`. It needs a container image with a shell, `git`, `python3` and `unshare`.
