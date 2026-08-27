# Webhook and outbound delivery semantics

## Guarantees implemented

- A signed Meta webhook is split into one durable Blob inbox item per seller message.
- Inbox names use opaque seller and message digests; a repeated Meta delivery creates the
  same Blob name and is not enqueued twice.
- Only the earliest pending message for each seller is selected. A Blob lease serializes
  competing workers, while different sellers may progress independently.
- Workflow writes use ETag optimistic concurrency, and processed message digests remain
  available after the five-minute conversational session expires.
- Before a seller instruction can mutate workflow state, the service atomically persists an
  in-flight message digest. If a process terminates and that digest survives, the next
  delivery is not executed automatically; the seller receives an uncertainty notice and
  must review/resend. This is an explicit at-most-once safety choice for commercial edits.
- Ordinary handler exceptions persist a replay barrier before the recovery message, so a
  known post-mutation delivery exception cannot automatically apply the mutation twice.
- Repeated inbox failures move the original payload to `dead-letter` after the configured
  delivery count. Dead-letter payloads are retained for controlled operator review; the
  application does not automatically replay them.
- Successful and dead-letter outcomes write an immutable terminal receipt before deleting
  the pending Blob. The enqueue path checks that receipt, preventing a later duplicate Meta
  webhook from recreating a terminal queue item.

## Residual crash window

The Blob inbox is durable **at least once**, while commercial execution uses a conservative
in-flight barrier to prevent automatic replay. The end-to-end system is still not
transactionally exactly once. Two irreducible windows remain:

1. A process can terminate after the in-flight claim but before applying the requested
   mutation. The restarted worker safely refuses to repeat it, so the instruction may need
   to be manually resent even though no change occurred. Conversely, a termination after
   mutation commit produces the same uncertainty path without duplicating the mutation.
2. Meta can accept an outbound text, image, or document and the process can terminate (or
   the HTTP response can time out) before local delivery state is saved. The conservative
   replay barrier avoids blindly resending the entire operation, but output may be omitted
   or only partly delivered. A multi-chunk response has no durable per-chunk completion
   ledger for resuming only the missing pieces.

Blob inbox, workflow ETags, or a receipt written after delivery cannot atomically cover an
external Meta API side effect. Marking an item complete *before* execution would change the
failure mode to silent message loss and is not an exactly-once fix.

Executable evidence for these windows is retained as expected-failure tests in
`tests/test_delivery_semantics_audit.py`.

## Required architecture for stronger delivery

A production exactly-once claim requires all of the following:

1. Each domain mutation and a deterministic outbound plan (operation ID, output ordinal,
   payload hash, recipient and media descriptor) must be committed in one optimistic-
   concurrency transaction.
2. A separate outbox worker must deliver each ordinal and persist its provider message ID.
3. The provider call must support a stable idempotency key, or the worker must be able to
   query/reconcile the provider by that operation key after an uncertain timeout.
4. Multi-part text and document upload/send steps need independent durable states.
5. Dead-letter replay must check the workflow receipt/outbox state first and must never be
   performed by blindly copying a Blob back to `pending`.

Terminal inbox receipts must be retained by the storage lifecycle policy for longer than
Meta's maximum operational redelivery/reconciliation window.

Without provider idempotency or reconciliation, the strongest honest choice is a documented
trade-off between at-least-once delivery (possible duplicates) and at-most-once delivery
(possible omissions). The service must not be described as exactly once until that external
constraint is solved.
