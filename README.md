# Traffic Signal Control with Reinforcement Learning

A deep Q network learns when to switch the lights at a single four way intersection so that
vehicles spend as little time waiting as possible. The intersection runs in SUMO and is
exposed to the agent through a Gymnasium environment.

The agent sees the queue length on each of the four approaches, which axis currently has
green, and how long that phase has been running. It chooses which axis holds green. A three
second yellow is inserted automatically whenever the choice changes.

## Requirements

**SUMO** must be installed separately, since it is not a Python package. Version 1.18 or
later is recommended.

- Ubuntu or Debian: `sudo apt install sumo sumo-tools sumo-doc`
- macOS: `brew install --cask sumo-gui`
- Windows: download the installer from https://sumo.dlr.de/docs/Downloads.php

After installing, set `SUMO_HOME` so that `traci` can find the simulator:

```bash
export SUMO_HOME=/usr/share/sumo        # adjust to your install path
export PATH=$PATH:$SUMO_HOME/bin
```

On Windows the installer normally sets `SUMO_HOME` for you.

**Python 3.9 or later**, then:

```bash
pip install -r requirements.txt
```

Verify the setup before running anything else:

```bash
sumo --version
python -c "import traci, gymnasium, torch; print('ok')"
```

## Project layout

```
traffic_rl/
├── env.py                 Gymnasium environment wrapping SUMO
├── agent.py               DQN agent, Q network and replay buffer
├── controllers.py         Fixed time, greedy and random baselines
├── demand.py              Random demand sampler used during training
├── simulation.py          Standalone episode runner for the baselines
├── make_scenarios.py      Writes the five evaluation scenarios
├── train.py               Trains on one fixed demand pattern
├── train_random.py        Trains on randomised demand, single seed
├── train_seeds.py         Trains five seeds on randomised demand
├── evaluate.py            Compares baselines against a single agent
├── evaluate_seeds.py      Compares baselines against all five seeds
├── test_env.py            Sanity check of the environment
├── test_minhold.py        Minimum green duration sweep
├── explore.py             Prints the signal programme and queue readings
├── plot.py                Training curves and the comparison chart
├── policy_map.py          Visualises the learned decision boundary
├── watch.py               Runs a controller in the SUMO window
├── sumo_files/            Network, routes and scenario configs
└── results/               Trained weights, trip outputs and figures
```

**Every script uses paths relative to the project root, so run them from inside
`traffic_rl/`.** Running `python traffic_rl/train.py` from the parent directory will fail
to find `sumo_files/`.

## Quick start

Watch the trained agent work, which is the fastest way to confirm everything is wired up:

```bash
cd traffic_rl
python watch.py reversed dqn
```

A SUMO window opens and the terminal prints queue lengths and the agent's value estimates
for each action. Try `python watch.py reversed fixed` to see the fixed timer handle the
same traffic, which is where the difference is most visible.

## Reproducing the results

The five scenario files are already in `sumo_files/scenarios/`, but you can regenerate them:

```bash
python make_scenarios.py
```

Train five seeds on randomised demand. This is the run behind the reported numbers and
takes a few hours, since it is 300 simulated hours in total:

```bash
python train_seeds.py
```

Weights land in `results/seeds/agent_seed0.pt` through `agent_seed4.pt`, with the per
episode waiting times in `results/seeds/history_seed*.txt`.

Evaluate every controller across all five scenarios:

```bash
python evaluate_seeds.py
```

This prints the comparison table and writes `results/seed_summary.txt`. Then draw the
figures:

```bash
python plot.py            # training curves and the comparison chart
python policy_map.py      # the agent's decision boundary in queue space
```

Everything is written to `results/figures/`.

## Other entry points

| Command | What it does |
| --- | --- |
| `python explore.py` | Prints the four phase signal programme and samples queue lengths every 60 seconds. Useful as a first look. |
| `python test_env.py` | Runs one episode with a greedy policy through the environment and reports total reward and mean wait. Checks the environment without touching the agent. |
| `python baseline.py` | Compares the fixed time, greedy and random controllers on the default demand. |
| `python train.py` | Trains for 30 episodes on one fixed demand pattern. Faster than the full run, but the resulting agent does not generalise. |
| `python train_random.py` | Trains for 60 episodes on randomised demand with a single seed. |
| `python test_minhold.py` | Sweeps a minimum green duration over the trained policy. This is the experiment that tests why the agent underperforms under heavy demand. |

## Known gaps

`evaluate.py` and `test_minhold.py` load `results/best_agent.pt` and
`results/random_agent.pt`, which are not included in this repository. Run `train.py` and
`train_random.py` first, or edit the path in those scripts to point at one of the seed
agents in `results/seeds/`.

The network file `sumo_files/intersection.net.xml` is committed, so you do not normally
need to build it. If you change the node, edge or connection files, rebuild with:

```bash
cd sumo_files
netconvert --node-files=intersection.nod.xml \
           --edge-files=intersection.edg.xml \
           --connection-files=intersection.con.xml \
           --output-file=intersection.net.xml
```

The generated signal programme has four phases: index 0 is green for north and south,
index 1 is the yellow that follows it, index 2 is green for east and west, and index 3 is
its yellow. The environment relies on that ordering, so keep it if you rebuild.

## Notes on the environment

- One episode is 3600 simulated seconds, roughly 720 decisions.
- The outer lane on each approach carries right turns and is left uncontrolled, so the
  agent observes and controls only lanes 1 and 2.
- The reward is the fall in total accumulated waiting time since the previous decision,
  divided by 100.
- `TrafficEnv(randomize=True)` resamples demand at every reset and writes it to
  `sumo_files/random.rou.xml`. This is what gives the agent its robustness to demand
  patterns it was not trained on.
- No minimum green is enforced inside the environment, so the agent is free to switch at
  every decision. `test_minhold.py` explores what happens when that freedom is removed.
