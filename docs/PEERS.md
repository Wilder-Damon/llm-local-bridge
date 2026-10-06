# Peer execution and handoffs

The `smith` and `claude` roles are peers. Either can originate requests, perform authorized
implementation, or independently review evidence. Role labels are not authentication,
rank, user authorization, or permanent reviewer assignments.

The original recipient owns task progress updates; the original requester closes it.
Claim tokens fence delivery attempts, not repositories, branches, or file edits across
separate requests. Agree on non-overlapping scopes or isolated checkouts and inspect existing
work before editing. A new message or expired claim does not authorize ownership takeover.

Renew before expiry if a reply is intended. After claim loss, stop using the token and
reconcile current ownership and artifacts before continuing. Do not repeat external work
merely because its acknowledgment is missing.

An explicit handoff preserves existing work and names the owner, exact revision, scope,
evidence, checks, unresolved issues and next step. The recipient checks actual user authority
and confirms ownership. Changed immutable scope/routing requires a new authorized request;
reference the old request in prose and use explicit status transitions as appropriate.
Never use a new request to evade the 12-reply limit or an approval gate.

Keep handoffs compact and refer to evidence rather than repeating transcripts. Fetch exact
unread/changed reviews, decisions, conditions and acceptance criteria before acting; previews
do not substitute for them. Receipt, acceptance, completion, independent review, publication
and merge are separate milestones. Stop repeated unproductive exchanges and involve the user.

Use the [changes feed and handoff template](CHANGES.md) for readable deltas. Pin input,
output and reviewed commits distinctly; `read --expected-commit SHA` can reject a stale
input before use. A mismatch is a reason to reconcile old findings, not discard them.
An `acked` delivery can still have task state `acknowledged`; claim/ack alone never means
the owner accepted or completed work. Record acceptance as an explicit `working` transition
and completion as reported evidence ready for review. Review verdicts need an exact SHA.

Capacity/reset/budget signals may be reported as optional timestamped prose with a source.
Unknown values remain unknown. No quota APIs, model routing, automatic delegation or scheduler
are implemented. Avoid duplicate scans and reviews; helpers must remain separately authorized.
