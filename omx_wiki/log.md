# Wiki Log

## 2026-11-09

- Fixed repeated instruction acknowledgments by moving static Telegram question guidance from per-turn user context to a registered system-prompt section. Preserved the question tool, clarify middleware and approval transport.
- Registration regression failed before the fix and passed afterward. Existing adapter tests passed in both maintained checkouts (environment-dependent cases skipped); the native Hermes PluginManager contract test passed. A controlled three-turn Hermes inference check confirmed two normal Korean greetings followed by the correct arithmetic result, with an unchanged system prompt.
- Existing local manifest and Wiki changes were preserved. No upstream merge or remote publication was performed.
