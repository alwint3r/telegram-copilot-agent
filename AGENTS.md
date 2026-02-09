# Repository Guidelines

## ExecPlan

When writing complex features, significant refactors, or being explicitly asked, use an ExecPlan (as described in .agent/PLANS.md) from design to implementation.

- Persist your ExecPlan inside the .agent directory alongside the file PLANS.md.
- Use unique name for your ExecPlan.
- Do not alter the .agent/PLANS.md file.


## Project Structure & Module Organization
This repository is a single-service Python bot with a small surface area:
- `chat-telegram.py`: main runtime (Telegram handlers, background dispatcher, Copilot session management, artifact sending).
- `chat-inline.py`: minimal local CLI loop for quick Copilot interaction checks.
- `test_copilot_session_manager.py`: unit and async integration-style tests using stubs/fakes.
- `docs/`: supporting design and operations docs (`architecture.md`, `configuration.md`, `testing.md`).
- `requirements.txt`: pinned runtime dependencies.

Keep new runtime logic in `chat-telegram.py` cohesive by extending existing domain types (`AskEventCollector`, `BackgroundDispatcher`) instead of adding parallel abstractions.

## Build, Test, and Development Commands
- Install deps: `pip install -r requirements.txt`
- Run bot locally: `python chat-telegram.py`
- Run inline CLI: `python chat-inline.py`
- Run tests: `python -m unittest -v`

Use a virtual environment for local development (`python -m venv .venv && source .venv/bin/activate`).

## Coding Style & Naming Conventions
- Follow existing Python style: 4-space indentation, type hints, `dataclass` for state containers.
- Use `snake_case` for functions/variables, `PascalCase` for classes, and explicit domain names.
- Prefer small helper functions for branching-heavy logic; keep call sites linear and readable.
- Keep comments concise and focused on intent, not restating code.

## Testing Guidelines
- Framework: built-in `unittest` (`unittest.TestCase` and `unittest.IsolatedAsyncioTestCase`).
- Add tests in `test_copilot_session_manager.py` near related test classes.
- Name tests as `test_<behavior>`.
- For regressions, include one test that reproduces failure and asserts the fixed behavior.

## Commit & Pull Request Guidelines
- Follow the observed commit style: Conventional Commit prefixes (`feat:`, `chore:`).
- Keep commits focused and explain user-visible behavior changes in the message body when needed.
- PRs should include:
  - concise problem/solution summary,
  - test evidence (`python -m unittest -v` output),
  - config/env changes (if any), especially new or modified variables in `docs/configuration.md`.

## Security & Configuration Tips
- Never commit secrets; set `TELEGRAM_BOT_API_KEY` via environment variables.
- Validate timeout and artifact limits through env vars instead of hardcoding defaults.
