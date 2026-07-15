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
