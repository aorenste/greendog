greendog is a tool for making it easier to investigate and fix master CI
failures on pytorch/pytorch.  Here is the design space we live in:

- The first iteration of the tool does NOT assume we have a working build of
  PyTorch that we can iterate on.  So we are basically looking for
  interventions that we can *one shot* without having the ability to locally
  test our changes.  This limits the set of potential interventions we can do,
  but that's good because we also want this tool to operate autonomously, and
  if we do complicated interventions it's more important for a human operator
  to intervene.

- We care about "situational awareness" about trunk.  E.g., consider all
  commits in the last 24 hours, what is not working (even if we can't easily fix it?)
  For example, pytorch/pytorch has a concept of ci: sev which is used to communicate
  breakage, we want our agents to have access to this info (example:
  https://github.com/pytorch/pytorch/issues/182227)  For example, the HUD view
  is intended to be a way for humans to visually understand trunk redness, but
  it has gone beyond human parseability.  Another important part of
  situational awareness is the periodic jobs, which we have far less signal
  on, it's much more important to sift out as much info as we can get from the
  logs.

- To add on, flakiness at scale is important, because if something keeps
  flaking at a nontrivial percentage, we should work on it.  We can think of
  stack ranking flakiness in terms of incidence in some period, and using that
  to prioritize work we want to do.

- Our agents do NOT have internet access, for security reasons.  The harness
  is responsible for feeding in information.

- The HUD at https://hud.pytorch.org/ has lots of useful information, in a
  sparsely documented API we have access to that is maintained by Dev Infra.  We should
  document and make use of it as appropriate.  For example, on green-red edges, it seems
  that we already have AI assessments about whether or not something broke master or not.
  These show up like https://github.com/pytorch/pytorch/actions/runs/25282086754 (advisor run).  But it seems these advisor runs don't always run.

- We can only easily test this live.  We'll work on features as we discover
  particular trunk breakages.

- There is an autorevert system.  I don't know how good it is.  We'll be
  evaluating how good it is as we work on this.
  https://hud.pytorch.org/hud/pytorch/pytorch/main/autorevert

- There are some configs that have been presistently broken.  If something's
  been broken for more than a week, let's maintain state about these as
  persistently broken, and we will need a dedicated stab to try to fix them.

- There are a HUGE number of configs. It will be important to subdivide the
  problem appropriately into subagents.
