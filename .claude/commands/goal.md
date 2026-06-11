---
description: Run a scoped BTC-arb task under standing discipline (diagnose-first, commit-gated, guardrailed)
argument-hint: <task -- diagnose-only or build, with explicit steps + any extra out-of-scope>
disable-model-invocation: true
---
# Scoped goal -- BTC PM arb

ENV (always): cwd MUST be the nested repo root C:\Users\mgill\Downloads\1-main\1-main
(outer path -> module-not-found). Use `py -3.12`, never bare python. ASCII-only stdout
(Windows cp1252).

DISCIPLINE (always):
- Diagnose-first: observe/probe and confirm the failure mode BEFORE changing any code.
- HARD GATE on the diagnosis: if the diagnosis contradicts the task's hypothesis, OR the
  failure mode does not reproduce, STOP -- write why to the findings file and END without
  implementing. Do not fix a problem you did not confirm. When the task defines a
  stop-on-pass condition (e.g. "if no violation is found, STOP"), honor it literally.
- If the task below is a DIAGNOSTIC (verify/check/measure): REPORT findings and STOP.
  Do NOT fix what you find; bring reds back for review.
- If it is a BUILD: committed steps, 1..N committed before N+1 begins; `py -3.12 -m pytest -q`
  GREEN on every commit; changes additive (numstat-proven); commit via temp file
  (write commit_msg.txt, then `git commit -F commit_msg.txt`).

EFFORT / ORCHESTRATION:
- DIAGNOSTIC (read-only audit/measure): subagent fan-out / dynamic workflows are
  welcome; parallel lanes gathering evidence. Read-only means "no mid-run input"
  costs nothing, so let it explore.
- BUILD (any edit/commit): NO auto-orchestration across a commit gate. Each committed
  step is one bounded unit; STOP after it for review before the next. No parallel
  agents editing source. A single careful chain at high/xhigh with a human checkpoint
  beats fan-out here; the commit-gate review IS the control.
- Goals run SEQUENTIALLY -- one /goal at a time, never concurrent sessions on this repo.
- Subagent fan-out has a session quota: prefer 4-5 parallel questions, not 6+.

HARD OUT-OF-SCOPE (always, regardless of task):
- Never loosen any floor / threshold / gate / fill model. Never clip or "rescue" #1
  chase-adjusted negatives; they are correctly-rejected losers, not missed signals.
- data/recordings/ is READ-ONLY: replay from it freely; never modify, rewrite, or
  delete recorded frames.
- The recorder/capture path is FROZEN mid-campaign: no edits to recording/capture code
  while capture windows are pending, regardless of what the task touches nearby.
- Do NOT touch the sibling repo polykal-prediction-agent.
- No git push unless the task explicitly says to push.

TASK:
$ARGUMENTS

END STATE: a single ASCII report of what was done/found, the pytest count, and the
working-tree/commit state. If anything blocked you, stop and report; do not improvise.
