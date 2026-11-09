# Hermes question guidance placement

The Telegram adapter's ordinary-question routing policy is static session policy. Register it with Hermes `register_system_prompt_section` under `codex-telegram.question-guidance`; do not return it from `pre_llm_call`.

Hermes appends string results from `pre_llm_call` to the latest user message. Putting standing instructions there makes a short greeting look like a fresh request to acknowledge those instructions and repeats them on subsequent turns.

Keep the existing question tool, clarify middleware, approval transport and completion hooks. The system section is rendered once for a new session, preserving the prompt-cache invariant. Existing conversation history must not be rewritten to remove old injected guidance.

Regression coverage lives in `tests/test_hermes_prompt_guidance.py` and the registration test in `tests/test_hermes_plugin.py`. The native check uses the real Hermes PluginContext and PluginManager to verify that both first and later turns receive no injected user context while the question policy remains present in a system section.
