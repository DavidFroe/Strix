## Compaction Handoff (Template — only relevant when the user invokes `/compact`)

**Do NOT volunteer this summary unsolicited.** This block is the *format
specification* the assistant must use when — and only when — the user runs
the `/compact` command. Outside of that command, ignore this section and
treat the visible conversation as the live transcript. **Never tell the
user that the conversation has been compacted unless you actually compacted
it on this turn.**

When `/compact` IS invoked, write the resulting summary in this shape into
`.deepseek/handoff.md`:

### Goal
[The user's high-level objective for this session]

### Constraints
[What's off-limits, what bounds the work, what the user explicitly does NOT want changed]

### Progress

#### Done
[What's complete and verified — landed commits, passing tests, shipped patches]

#### In Progress
[What's mid-flight — partial implementations, open PRs, work-in-tree]

#### Blocked
[What's stuck, why, and what would unblock it]

### Key Decisions
[Architectural choices, design decisions, trade-offs made — the WHY behind the work]

### Next step
[The single next action to take when resuming — one line, concrete]
