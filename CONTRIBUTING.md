# Contributing to CowAgent

Thanks for taking the time to contribute! 🎉 CowAgent is built by a global
community, and contributions of all sizes are welcome — from typo fixes to new
features.

## Language policy

To keep the project accessible to a global community, **please write issues,
pull requests, code comments, and commit messages in English.**

> 为方便全球开发者协作，请尽量使用**英文**提交 issue、PR、代码注释与
> commit message。不必担心英文不完美——表达清楚即可，工具翻译也完全没问题。感谢理解 ❤️

## Reporting issues

Found a bug or have an idea? [Open an issue](https://github.com/zhayujie/CowAgent/issues/new/choose).

Before opening one, please search existing issues (including closed ones) to
avoid duplicates, and make sure you're on the latest version.

## Submitting a pull request

1. **Fork** the repo and create a branch from `master`
   (e.g. `feat/web-search`, `fix/telegram-reconnect`).
2. Make your change. Keep it focused — one logical change per PR.
3. Follow the existing code style. Write comments and docstrings in English.
4. Run the app locally to confirm your change works.
5. Open a PR with a clear title and a short description of **what** and **why**.

We keep the bar friendly: clear, focused, and working is enough. Maintainers are
happy to help polish details during review.

### Commit & PR titles

Use a short, imperative summary. The [Conventional Commits](https://www.conventionalcommits.org/)
style is preferred but not required:

```
feat: add web search tool
fix: reconnect Telegram websocket on timeout
docs: clarify Docker setup
```

## Development setup

See the [Install from Source](https://docs.cowagent.ai/guide/manual-install)
guide. In short:

```bash
git clone https://github.com/zhayujie/CowAgent.git
cd CowAgent
pip install -r requirements.txt
pip install -e .
cow start
```

## EcoreX release validation


## CowAgent data-plane compatibility boundary

e-Mate keeps its own desktop UI and enterprise control plane, but its Agent
data plane follows upstream CowAgent 2.1.5
(`e3ac1b952500f60934862c6bf0bd0de91b415ed8`) as the behavioral baseline.

- The enterprise control plane may manage only accounts/passwords, Token usage,
  version publication, and audit records.
- Account isolation, release-artifact integrity, Token accounting, and audit
  collection must observe the Agent data plane without changing which tools
  the model sees, their JSON schemas, their execution lifetime, or their
  results.
- First-party tools are available out of the box. Do not add permission
  profiles, “default/full access” modes, per-tool approval prompts,
  administrator tool-deny lists, intent-keyword hiding, profile-dependent tool
  availability, or a second schema interpreter.
- A Tool contract has one source of truth from model schema through execution.
  Adapters and Packs may transport a call, but must not narrow or reinterpret
  its accepted arguments.
- Preserve CowAgent lifetimes and semantics: the browser is stateful across
  tool calls in one Runtime, navigation returns a usable snapshot, later
  actions do not require another URL, Shell is directly callable, Fetch returns
  readable content, and Web Search is a first-class capability.
- Release verification happens when an immutable tool artifact is admitted or
  loaded. Do not re-verify or add policy decisions after every ordinary tool
  call unless a concrete integrity failure proves that boundary necessary.
- Do not invent a stricter e-Mate behavior “for safety.” A deviation from
  CowAgent requires a reproduced OS/platform, provider, legal, or data-loss
  constraint plus the smallest regression that proves it. The deviation must
  be documented at the exact boundary and must not be controlled remotely by
  the enterprise management plane.
- Frontend labels and controls must reflect the same boundary. Do not expose a
  permission switch that has no CowAgent equivalent.

Any change touching tool discovery, schemas, prompts, execution, browser
lifetime, Shell/Fetch/Search, or result envelopes must run a CowAgent-parity
regression. Before a frozen release, the current-platform development candidate
must also prove context continuity, real Web Search, and the complete default
capability catalog.


### Channels, Skills, MCP, and scheduling

The CowAgent baseline also covers channels, Skills, MCP, and scheduled tasks.

- Channels use CowAgent connection, receive/send, retry, and reconnect
  semantics. e-Mate may keep its existing user-facing channel display names,
  icons, and layout, but those presentation names must not select a different
  transport or runtime policy.
- Skills are discovered and loaded from the user's local Skill directories as
  CowAgent does. Cloud catalogs, signatures, revisions, or discovery receipts
  may provide optional distribution metadata but must not gate a valid local
  Skill.
- MCP uses CowAgent's local configuration, lazy server startup, OAuth flow, and
  dynamic tool registration. Enterprise policy must not hide or rewrite an MCP
  tool contract.
- Scheduled tasks use CowAgent's local Scheduler tool and durable local task
  store. They do not require an enterprise approval or remote policy lease to
  run.
- e-Mate adapters may translate presentation and event envelopes only. They
  must not fork the underlying CowAgent state machine or retry semantics.

### Memory and knowledge

- `MEMORY.md`, `memory/**/*.md`, and `knowledge/**/*.md` are the user-owned
  source of truth. The Agent tools, prompt context, and the e-Mate Memory and
  Knowledge pages must read the same files; do not copy them into a competing
  canonical store for ordinary execution.
- Keep CowAgent's `memory_search` and `memory_get` contracts. Recall is
  mandatory for past decisions, preferences, relationships, and to-dos.
- Restore CowAgent's proactive writes: durable preferences and decisions go to
  `MEMORY.md`, daily progress to `memory/YYYY-MM-DD.md`, and structured sources,
  analysis, entities, and concepts to matching `knowledge/` pages. Updating a
  knowledge page also updates `knowledge/index.md`.
- Memory and knowledge writes use the ordinary CowAgent file tools. Do not add
  a knowledge request, approval, remote policy lease, revision ticket, or
  administrator gate to the model path.
- Never store credentials, passwords, API keys, or Tokens in memory files.

For everyday development and release-candidate preflight, run the lightweight
real-release check:

```bash
python scripts/真实发布轻量校验.py
```

After version freeze, generate the multi-agent split plan if several agents
will collect evidence in parallel:

```bash
python scripts/真实发布多Agent分工策略.py
```

If the full gate fails, generate a focused rerun plan before spending time on
another full pass:

```bash
python scripts/真实发布失败复验策略.py
```

After a release candidate has been deployed to the real production server, and
before public promotion, ask the operator whether to run the full real release
validation:

```bash
python scripts/真实发布校验.py
```

Do not run the full gate automatically: it connects to production and may call
real models, image generation/editing, and concurrency pressure tests.
Do not run multiple full gates in parallel; the split plan is for evidence
collection only, and the final full gate remains the release-blocking source of
truth.
Focused reruns are proof-of-fix evidence only; batch fixes and run the final
full gate once before promotion.

## Code of conduct

Be respectful and constructive. We want CowAgent to be a welcoming place for
everyone.
