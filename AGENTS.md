# Agentic Coding Best Practices

These rules apply to every coding task in this workspace. Treat them as working constraints, not optional advice.

## 1. Start With Evidence

- Restate the concrete objective, affected scope, constraints, and completion criteria before substantial work.
- Inspect the repository, nearby code, tests, configuration, and current working-tree state before proposing changes.
- Search narrowly first. Read relevant files and sections instead of loading the whole repository or dumping large outputs into context.
- Distinguish facts from hypotheses. For bugs, form testable hypotheses and use logs, code, tests, or reproduction steps to eliminate them.
- Ask only when a missing decision materially changes the implementation or creates unacceptable risk. Otherwise make a bounded assumption and state it.

## 2. Plan Proportionally

- For a small, local, reversible change, act directly after inspection.
- For multi-file, architectural, migration, security-sensitive, or ambiguous work, write a short plan with explicit verification steps before editing.
- Use a durable specification for long or cross-session work: requirements, design decisions, tasks, acceptance criteria, and current status must live outside chat history.
- Keep tasks small enough to verify independently. Prefer vertical, working slices over broad unfinished scaffolding.
- Re-check the original objective after major discoveries and before declaring completion.

## 3. Manage Context Deliberately

- Context is a scarce working memory, not an archive. Keep high-signal project facts, current decisions, and the active plan visible; omit repeated or irrelevant output.
- Prefer targeted search, bounded file reads, pagination, filters, and summaries over full data dumps.
- Use ignore files and tool filters to exclude generated files, dependencies, caches, binaries, and unrelated directories.
- Batch related independent reads or checks when this reduces round trips without making results harder to interpret.
- Record important decisions, unresolved findings, and next steps in repository artifacts or task tracking rather than relying on conversation memory.
- For a long task, periodically compress status into: goal, completed work, evidence, remaining work, risks, and exact next action.
- Start a fresh task/context when the objective changes materially; do not let unrelated history pollute a new problem.

## 4. Make Minimal, Coherent Changes

- Preserve existing architecture, conventions, public behavior, and user changes unless the task explicitly requires altering them.
- Change the smallest coherent surface that solves the root cause. Avoid opportunistic refactors, formatting churn, dependency upgrades, or generated-file changes.
- Reuse existing abstractions when they fit. Do not add a framework, layer, or dependency for a one-off need.
- Keep interfaces consistent across all consumers. When an interface changes, update its implementation, callers, tests, types, and documentation as one semantic change.
- Prefer readable, explicit code over clever code. Add comments only for non-obvious intent, invariants, or tradeoffs.
- Treat error paths, empty states, boundaries, concurrency, retries, cancellation, and compatibility as part of the implementation, not follow-up polish.

## 5. Close the Feedback Loop

- Work in an inspect -> hypothesize -> change -> verify loop. Never treat code generation as proof of correctness.
- Run the narrowest relevant check first, then broaden in proportion to risk: focused test, related suite, lint/typecheck, build, integration or end-to-end checks.
- Add or update tests for changed behavior and regressions when the repository has a testing path.
- Prefer deterministic, machine-readable diagnostics. Preserve useful exit codes, structured errors, and concise actionable messages.
- When a check fails, read the complete relevant error, identify whether it disproves the approach, and adjust deliberately. Do not stack speculative patches.
- If verification cannot run, state exactly what was not run, why, what evidence is available, and the residual risk.
- Completion requires evidence against the acceptance criteria, not confidence or a plausible-looking diff.

## 6. Use Tools as Agent Interfaces

- Select tools by user intent and workflow, not by a one-to-one mapping from underlying REST endpoints.
- Prefer a small set of high-value, non-overlapping tools. Similar names or descriptions create selection errors and consume context.
- Give each tool a short, verb-led name and a description that states when to use it, when not to use it, required preconditions, side effects, and important limitations.
- Keep parameters semantic, typed, and constrained. Use enums, defaults, examples, and validation; avoid deeply nested schemas and opaque identifiers when meaningful values are available.
- Provide workflow-level operations for repeated multi-step sequences, while retaining lower-level primitives needed for composition and diagnosis.
- Return concise, meaningful results by default. Support filtering, pagination, truncation notices, and optional detailed output.
- Errors must explain what failed, why it failed, whether retry is safe, and the next corrective action. Do not return raw stack traces as the only guidance.
- Include diagnostic or info modes for environment, identity, permissions, versions, and configuration when those factors commonly cause failures.
- Use the platform's native function/tool-calling mechanism instead of inventing a prompt-only calling syntax.
- Evaluate tools with realistic tasks and logs. Refine names, descriptions, parameters, and outputs based on observed selection and execution failures.

## 7. Choose the Right Reusable Artifact

- Put stable, universal, low-conflict project constraints in `AGENTS.md` so they apply automatically.
- Put directory-, language-, framework-, or domain-specific constraints in the narrowest applicable nested `AGENTS.md` or equivalent scoped rule.
- Create a Skill for a repeatable workflow that should load only for relevant requests, especially when it needs detailed guidance, references, scripts, or templates.
- Create a plugin only when distribution requires a capability bundle such as Skills plus MCP servers, apps, hooks, commands, or other runtime integration.
- Keep one responsibility per rule or Skill. If its purpose cannot be stated in one sentence, split it.
- Move rare, high-risk, or high-cost procedures out of always-on rules and require explicit invocation or confirmation.
- Establish a no-Skill baseline before automating a workflow; design the smallest Skill that fixes observed failures.
- For Skills, make trigger metadata precise, keep the main instructions lean, load detailed references progressively, and move deterministic repeated actions into tested scripts.
- Validate reusable artifacts with realistic success, edge, and failure cases. Version, revise, merge, or retire them when they become noisy or obsolete.

## 8. Protect the Workspace and User

- Inspect `git status` before editing. Treat pre-existing modifications and untracked files as user-owned.
- Never discard, overwrite, or reformat unrelated user work. Stop and ask if safe integration is not possible.
- Avoid destructive commands and irreversible operations unless explicitly requested and confirmed.
- Apply least privilege to credentials, network access, external systems, package installation, and deployment.
- Separate read/diagnostic actions from write actions. Preview high-impact operations and make side effects explicit.
- Do not expose secrets in code, logs, command output, commits, or final responses.
- Make retries idempotent where possible; guard against duplicate writes and partial completion.

## 9. Keep Version History Trustworthy

- Isolate concurrent agent tasks with separate branches or worktrees when they can overlap. Do not let multiple agents edit the same working tree without coordination.
- Protect the main branch and require relevant CI checks; agents must not bypass branch protection or quality gates.
- Keep commits atomic: one complete semantic change per commit, including inseparable caller/test updates.
- Separate unrelated formatting, refactoring, dependency, and behavior changes.
- Write commit and PR descriptions that explain intent, scope, important decisions, verification, risks, and any agent involvement required by the repository.
- Prefer small, reviewable PRs. Use stacked changes when a large task has ordered, independently reviewable layers.
- In monorepos, use dependency/affected analysis to select checks instead of blindly running every package, while ensuring shared-interface changes update all affected consumers.
- Do not create commits, branches, PRs, or push changes unless the user requests it or the established workflow clearly authorizes it.

## 10. Communicate for Handoff

- During long work, provide concise progress updates when assumptions, scope, risks, or findings change.
- Lead the final response with the outcome. Identify changed files and behavior, then list verification performed and any remaining risks.
- Do not claim success when required work remains or tests are failing.
- Report noteworthy issues discovered but intentionally left out of scope so they are not lost.
- Suggest next steps only when they are natural and actionable.

## Rule Maintenance

- Add a rule only after identifying a recurring failure, constraint, or coordination need.
- Keep rules short, concrete, testable, and action-oriented; replace vague principles with observable behavior.
- When rules conflict, prefer the more specific rule and the one closest to the affected files, unless a higher-priority instruction says otherwise.
- Remove or narrow rules that repeatedly trigger in unrelated tasks, duplicate stronger instructions, or no longer match the project.
- Review this file as an engineering asset. More rules are not automatically better; clear boundaries and correct activation are the goal.