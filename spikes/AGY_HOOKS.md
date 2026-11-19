## Antigravity (agy 1.5.12) hooks + inbound delivery — spike 2026-11-18

Measured with a capture-only hook in ~/.gemini/config/hooks.json (since removed; file restored byte-identical)
and an interactive agy in a private tmux server.

1. The GLOBAL ~/.gemini/config/hooks.json fires for the CLI, for EVERY agy session on the host (a live session
   of the operator's went through the spike hook too). Hooks are synchronous and block agy's loop.
2. Every payload: conversationId, workspacePaths, transcriptPath, artifactDirectoryPath, modelName (e.g.
   "gemini-4-hydrogen-medium"), lastUserInput, cwd, hookEventName. Event extras:
   - SessionStart: none (lastUserInput already set in -p mode)
   - PreInvocation / PostInvocation: invocationNum (0 = a turn's FIRST model call), initialNumSteps
   - PreToolUse / PostToolUse: toolCall {name, args, id}, stepIdx (PostToolUse adds error when the tool failed)
   - Stop: terminationReason (NO_TOOL_CALL = normal end), fullyIdle, executionNum, error, continuationActive
   Order of one turn: SessionStart → PreInvocation(0) → PreToolUse → PostToolUse → PostInvocation(0) →
   PreInvocation(1) → PostInvocation(1) → Stop.
3. **PreToolUse MUST answer an explicit decision.** `{}` DENIES the tool ("tool call denied by pre-tool hook").
   `{"decision":"ask"}` is neutral: tools agy would auto-approve still run unprompted (print mode, auto mode).
   Every other hook may answer `{}`.
4. Mid-turn delivery works: PreInvocation answering `{"injectSteps":[{"userMessage":"..."}]}` reached the model
   (the reply obeyed it). PreInvocation only fires while a turn is running — an idle session gets nothing.
5. Idle delivery works by tmux into agy's pane, exactly like Claude's: `send-keys -l <text>`, ~1 s, `send-keys Enter`
   submitted it; a MULTI-LINE message must go in as a bracketed paste (`load-buffer` + `paste-buffer -p`), which
   stays in the input box until Enter (plain newlines would submit early). A first launch in an untrusted folder
   shows a trust dialog first — delivery must only target a pane at agy's input prompt.
6. `agy remote-control` is a daemon for remote (cloud) control, not a local input channel; not used.
