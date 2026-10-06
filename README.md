# pocketful-factory

A three-seat software factory for the WeAreDevelopers x BAND Dark Factory hackathon, pocketful track. A human pastes one task into a BAND room. Three coding-agent seats plan the work, build it, and check it independently, with no further human input. The factory is the mandates in `mandates/`. The product it built is in `stage-1/`.

## The seats

| Seat | Owns | Cannot |
|---|---|---|
| `foreman` | Plan, work items, acceptance criteria, `FACTORY.md`, closing the stage | Write service code, accept "done" from the builder |
| `builder` | Service code, tests, build file, fixing reported defects | Change the plan, declare its own work correct |
| `assayer` | PASS or FAIL per work item, adversarial scripts in `stage-N/verify/` | Edit service code, pass on a claim instead of a command's output |

Routing is by @mention, the only way BAND delivers a message to a seat:

1. Human to foreman: the stage task, once.
2. Foreman to builder: one work item with acceptance criteria.
3. Builder to foreman: ready, with changed files, build and run commands, test output.
4. Foreman to assayer: what to verify and how to run it.
5. Assayer to foreman on PASS. On FAIL, one message to builder and foreman with the failing command and a reproduction script.
6. After three failed rounds on one item the foreman rewrites it smaller and logs why in `FACTORY.md`.
7. The foreman closes the stage only when every requirement has a PASS and its own final check, run with the network disabled at the operating-system level, succeeds.

The assayer's veto is the reason the room exists: the foreman counts rounds and closes only on an assayer PASS, and the builder never judges itself.

## The mandates are generic

The mandates name no endpoint, field, error code, or domain term. All track detail is in the task the human sends (`tasks/stage-1.md`). The same three files could drive a team building something else.

## Layout

```
mandates/        the three standing instructions, one per seat
tasks/           the exact message sent to the room for each stage
stage-1/         the service built in stage 1, with its own README, tests, container build file, verify/ scripts
FACTORY.md       plan, assumptions, rounds, failures and recoveries, usage and cost
SETUP.md         how the seats were created and configured
room-export/     export of the BAND room that produced stage-1/
```

## Stage 1 result

- All four work items passed independent verification in round 1: the service and read API, money mutations, splits and feed, and the delivery audit. No round failed, so this run has no caught-and-fixed defect to show.
- Verification is the assayer's three scripts in `stage-1/verify/`, each run in a fresh user and network namespace, plus 17 unit tests.
- The seats run inside containers with no container engine, so their check of the `Dockerfile` was static. The image was built and served with `--network none` afterward, outside the factory, and answered `/health`, user creation, deposits, a replayed idempotent write, a payment, an overdraft rejection and a split.
- About 40 minutes passed from the task message to the foreman closing the stage.

## Measured usage

Token totals for the room that produced `stage-1/`, read from each seat's final session token count:

| Seat | Total tokens |
|---|---|
| foreman | 1,665,728 |
| builder | 5,403,287 |
| assayer | 2,847,688 |
| all three | 9,916,703 |

Of the total, 9,831,710 are input tokens (9,498,112 of them cached) and 84,993 are output tokens. There is no dollar figure: `band usage` does not attribute these sessions, so the log in `FACTORY.md` records the cost as unmeasured. Three earlier attempts in other rooms were abandoned and used another 2,462,430 tokens that are not in this table.

## Running stage 1

See `stage-1/README.md` for the service's API and run commands. To build and serve it in a container:

```sh
podman build -t pocketful stage-1
podman run --rm -p 8080:8080 -v pocketful-data:/data pocketful
curl -s http://127.0.0.1:8080/health
```

The service makes no outbound calls. To check that, run the container with `--network none` and call it from inside, since Podman discards published ports in that mode:

```sh
podman run -d --rm --network none -v pocketful-data:/data --name pf pocketful
podman exec pf python3 -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8080/health').read().decode())"
podman stop pf
```

The same commands work with `docker`.

## Seat setup and cost

`SETUP.md` has the commands, the runtime, the model and the settings used for the run. `FACTORY.md` is the factory's own log.
