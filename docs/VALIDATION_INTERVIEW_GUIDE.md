# Distributor Validation Interview Guide

See [ROADMAP.md](ROADMAP.md#validation-gates) — this operationalizes the
**direct field validation** gate that Phases 7–11 are waiting on. It has
not been done yet. This guide exists so it gets done rigorously, with
comparable evidence across prospects, rather than as a handful of
informal chats whose conclusions are hard to weigh against each other
later.

**What "done" looks like:** a few (not one) Lusaka-area FMCG
wholesalers/distributors, matching the [ICP](REQUIREMENTS.md#3-initial-customer-profile-icp),
each scored against the same three criteria below, with notes specific
enough that a decision to proceed (or not) doesn't rely on memory or
vibes six weeks later.

## Before you go: screen for ICP fit

Don't spend a full interview slot on a business that's obviously outside
the target segment — a two-minute phone screen first:

- Lusaka-based (or Lusaka-adjacent)?
- FMCG wholesale/distribution — drinks, groceries, hardware, cosmetics,
  electrical, spares, or agri-inputs?
- Roughly 3–20 sales staff, 1–3 warehouses?
- Do their customers already order repeatedly over WhatsApp (even
  informally, even if it's chaos today)? This is the one that matters
  most — everything else in the pitch depends on the ordering channel
  already existing.

If the answer to the WhatsApp-ordering question is no, this isn't a
disqualifier for reconciliation-only interest, but it does mean the
*order-to-cash* pitch (the actual product direction) doesn't apply yet —
note that explicitly rather than scoring the interview as a clean pass or
fail.

## The three validation criteria

Ask about each one specifically — don't let the conversation stay
general. Record a clear yes/no/unclear plus the specific evidence, not
just an impression.

### (a) Do they have exportable invoices and statements at all?

- How do they currently track what's owed to them — spreadsheet,
  notebook, accounting software, nothing formal?
- Can they actually export or hand over an invoice list and a MoMo
  statement in a file (CSV/Excel), or would someone have to manually
  retype everything first?
- If they use MTN/Airtel MoMo for business collections, can they pull a
  statement themselves, or does that require going through an agent/branch?

**Evidence to capture:** what format their real data is actually in.
If it's "a notebook," that's a **no** on this criterion, not a "sort of" —
this tool reads structured files, and getting there is a real, separate
piece of work worth knowing about upfront.

### (b) Do customers pay into a small number of *business-owned* MoMo lines, not individual salespeople's personal numbers?

This is the one criterion the README calls out as unfixable by this
tool's matching logic if it's false — worth being direct about.

- When a customer pays, whose MoMo number does the money land in? The
  business's own line, or whichever salesperson took the order that day?
- How many MoMo lines does the business actually collect payment through?
  (One is ideal; a handful is workable; "every salesperson has their own"
  is the failure mode.)
- If it's currently the failure mode, would the business be willing to
  consolidate onto a small number of business-owned lines? (This is an
  organizational/trust question for them, not a technical one — probe
  for whether it's even conceivable, not just whether it's true today.)

**Evidence to capture:** a real count of how many distinct numbers
customer payments land in, and whose they are.

### (c) Is manual reconciliation costing them real hours or real money — not just mild annoyance?

- Who does reconciliation today, and how long does it actually take per
  week/month? (Get a number, not "a while.")
- Has a payment ever gone unmatched to an invoice and just... stayed
  that way? What happened as a result?
- Has this ever caused a real dispute with a customer, a cash-flow
  surprise, or a written-off amount?
- If reconciliation took a tenth of the time, what would that person do
  with the freed-up hours — is there an actual answer, or is it not a
  real constraint on the business?

**Evidence to capture:** a concrete number (hours/week, a Kwacha amount
lost to unmatched payments, or a specific incident) — "it's annoying but
fine" is a genuine answer and should be recorded as a **weak** pass, not
rounded up to a strong one.

## Show them the tool, don't just ask about the problem

Per the product statement in [REQUIREMENTS.md](REQUIREMENTS.md#2-product-statement),
lead with the boring truth, not the technology:

> "Turn WhatsApp orders into invoices and automatically reconcile the
> payments."

Walk through the actual running app against the shipped `sample_data/` —
upload an invoice list and a MoMo statement, show the Matched/Partial/
Needs Review/Unmatched split, show the aging view. If they're far enough
along in the conversation, show a WhatsApp order message turning into an
invoice. Watch for:

- Do they recognize their own mess in the "before" description (the
  twelve-step manual process), or does it not quite match their
  situation?
- Does the Needs Review concept land as trustworthy ("good, it asks a
  human instead of guessing") or as a gap ("why didn't it just figure it
  out")? Both are useful signals about what this customer actually values.
- Any visible reaction to the word "AI" if it comes up — confirms or
  disproves the "lead with the boring truth" positioning bet.

## Scoring and what to do with it

For each prospect, record:

| Criterion | Yes / Weak / No | Evidence |
|---|---|---|
| ICP fit (WhatsApp ordering already happening) | | |
| (a) Exportable invoices/statements | | |
| (b) Business-owned MoMo lines | | |
| (c) Real cost of manual reconciliation | | |

- **All three strong yes + ICP fit:** a genuine pilot candidate — this is
  what "done" looks like for the validation gate on a per-prospect basis.
- **(b) is a clear no:** per the README, this is an organizational
  problem this tool can't solve — don't pilot here until/unless that
  changes, regardless of how strong (a) and (c) are.
- **(a) or (c) is weak:** still worth noting, but weigh it against how
  many prospects show the same pattern before treating it as a dealbreaker
  for the segment as a whole rather than just this one business.

The gate in [ROADMAP.md](ROADMAP.md#validation-gates) isn't "one good
conversation" — it's a *few* of these, compared side by side, before
Phase 7 (warehouse), Phase 8 (delivery), Phase 9 (analytics), Phase 10
(live MoMo webhooks), or Phase 11 (ZRA fiscalization) get built.
