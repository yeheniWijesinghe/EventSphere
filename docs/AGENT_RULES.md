# EventSphere – agents, messages and decision rules

All constants and the human-readable text of every rule live in `app/agents/rules.py` (`RULES`).

## What makes this agent-based (and the creative parts)

1. **Explainable agents.** Every reply an agent sends carries `rule` (e.g. `B3`), `rule_text` and, where useful, `evidence` (the beliefs it used, e.g. `{"requested": 7, "limit": 6}`). Users see a "Why?" box, the scanner shows the rule, the message log has a Rule column, and each agent counts how often it fires each rule (`rules_fired` belief).
2. **HirerAgent: a personal agent that negotiates.** The attendee gives a budget and how flexible they are. Their HirerAgent asks the VenueAgent for every feasible slot, then haggles with alternating offers (PROPOSE → counter-PROPOSE → AGREE) before booking.
3. **Agent mood / workload.** Each agent tracks how many messages it handled in the last minute and reports itself as *calm* (< 10), *busy* (10–29) or *overloaded* (30+). The BookingAgent also adapts to demand: rule B12 tightens the per-booking limit when a ticket category is nearly sold out.
4. **Live conversation diagram.** `/admin/conversations/{id}` replays one conversation as an animated sequence diagram of the messages between agents, with performative colours and rule IDs.

## Message format (FIPA-ACL style)

```json
{
  "sender": "BookingAgent",
  "receiver": "TicketAgent",
  "performative": "REQUEST",
  "content": {"action": "issue_tickets", "booking_id": 12},
  "conversation_id": "conv-3f9a1c0b2e",
  "in_reply_to": null
}
```

* Performatives: `REQUEST`, `AGREE`, `REFUSE`, `PROPOSE`, `INFORM`, `FAILURE`.
* The `Message` Pydantic model **rejects** a `REFUSE`, `PROPOSE` or `FAILURE` without a `reason`, and a `PROPOSE` without a non-empty `alternatives` list.
* Agents only talk through `MessageBus.send()` (point-to-point, returns the reply) and `MessageBus.broadcast()` (INFORM to subscribers). No agent calls another agent's methods.
* Every message, including replies and broadcasts, is written to `agent_messages` with its conversation id. You can see it at **/admin/messages**. Click a conversation id to follow one user action across agents.
* Beliefs are kept in memory and written to `agent_states` (JSON) after every handled message. They are reloaded the first time an agent runs after a restart.

## VenueAgent: keep every venue's calendar free of clashes

| ID | Rule |
|----|------|
| V1 | Two activities at one venue need a **30-minute turnaround buffer**: a slot clashes if `start < other.end + 30 min` and `end > other.start − 30 min`. |
| V2 | Total tickets of an event may not exceed the venue capacity. If they do: REFUSE. |
| V3 | An event slot must start in the future and end after it starts. If not: REFUSE. If it clashes: PROPOSE free slots (or REFUSE if none are found). |
| V4 | Hire requests need guests ≥ 1, a purpose, and an end after the start. If not: REFUSE. |
| V5 | Private hire is only allowed between 07:00 and 23:00 on a single day. |
| V6 | Hire length must be 1–12 hours. If not: REFUSE. |
| V7 | Hire must be requested at least 24 h and at most 180 days in advance. |
| V8 | If a hire breaks V5, V7, the capacity rule, the "open for hire" flag or V1, the agent **PROPOSEs** alternatives instead of refusing: the same venue earlier or later that day, the same time on following days (up to 14 days, max 3), plus up to 2 other open venues big enough for the group. It REFUSEs only if no alternative exists. |
| V9 | Price = hourly rate × hours, worked out in 30-minute slices. Saturday/Sunday slices ×1.25; slices from 18:00 ×1.20 (they multiply together). |
| V10 | **Escalate** to an admin (reply `INFORM`, status `PENDING_APPROVAL`, slot provisionally held) if guests > 80 % of capacity, length > 6 h, or price > Rs. 100,000. Otherwise **AGREE**: confirmed automatically. |
| V11 | When an admin approves, the agent re-checks clashes. It REFUSEs if the slot is no longer free. |

Beliefs: counts of slot checks, accepted/refused/countered slots, confirmed/escalated/countered/refused hires, approvals, total quoted, freed slots.

| V12 | Negotiation: the VenueAgent's floor is the list price minus 15 % for weekday daytime (off-peak) slots, or minus 5 % for weekend/evening slots. It AGREEs to any bid at or above its floor. Otherwise it counter-PROPOSEs halfway between its last ask and the bid (never below the floor). An agreed price is only honoured for the same slot in the same conversation. |

## BookingAgent: sell tickets fairly without overselling

| ID | Rule |
|----|------|
| B1 | Only PUBLISHED events can be booked. |
| B2 | Sales close when the event starts. |
| B3 | At most **6 tickets per booking**. |
| B4 | At most **10 active tickets per person per event**. |
| B5 | Seats are taken with an atomic conditional update: `UPDATE ticket_categories SET sold = sold + q WHERE id = :id AND sold + held + q <= quantity`. 0 rows updated means not enough seats. A DB `CHECK (sold + held <= quantity)` backs this up. |
| B6 | All-or-nothing across categories: if one category fails, seats already taken are released. The reply is a PROPOSE offering a smaller quantity (if any seats are left) and the waitlist. |
| B7 | Simulated payment: `SIM_CARD`/`SIM_WALLET` succeed with reference `SIM-YYYYMMDD-XXXXXXXX`. `SIM_DECLINE` is refused and the seats are released. |
| B8 | Refund on attendee cancellation: ≥ 7 days before = 100 %, ≥ 48 h = 50 %, otherwise 0 %. No cancellation after the start or once a ticket has been used. |
| B9 | Organiser cancels an event: every booking gets a **100 %** refund and every ticket is voided. Broadcasts `event_cancelled`. |
| B10 | After a cancellation the agent broadcasts `seats_released` so the WaitlistAgent can react. |
| B11 | Seats held for the waitlist (`held`) can't be bought by the public. Only `book_from_hold` turns held seats into sold seats. |
| B12 | **Scarcity rule:** once a category is 90 % sold or held, one booking may take at most 4 of it. The agent PROPOSEs the smaller quantity instead. |

Beliefs: bookings accepted/refused, refusal reasons, tickets sold, revenue, refunds, cancellations, check-ins per event (from TicketAgent INFORMs).

## TicketAgent: admit each ticket exactly once

| ID | Rule |
|----|------|
| T1 | Codes come from `secrets.token_urlsafe(18)` (~144 random bits) and are unique. A ticket can go VALID → USED once only. |
| T2 | Unknown code → REFUSE `INVALID`. |
| T3 | Cancelled ticket → REFUSE `CANCELLED`. |
| T4 | Ticket for another event → REFUSE `WRONG_EVENT` (names the other event). |
| T5 | Check-in opens 180 min before the start and closes at the end → otherwise REFUSE `OUTSIDE_WINDOW`. |
| T6 | 3 or more scans of the same code within 10 minutes → flagged as **suspicious** (possible shared screenshot). The agent broadcasts `suspicious_scan` once, and admins are notified. |

Each admit sends an INFORM `ticket_checked_in` to the BookingAgent. Beliefs: issued, voided, admitted, rejections by reason, recent scans per code (pruned to 10 min), suspicious codes.

## WaitlistAgent: pass freed seats to waiting customers

| ID | Rule |
|----|------|
| W1 | Only published events that haven't started yet. |
| W2 | 1–4 seats per waitlist entry, one active entry per person per category. |
| W3 | Joining while tickets are still available → PROPOSE "book directly". |
| W4 | Freed seats are **held for 10 minutes** (`WAITLIST_HOLD_MINUTES`), never past the event start. The attendee sees a live countdown. |
| W5 | Who gets the seats: first come, first served. Anyone who wants more seats than are free is skipped but keeps their place. |
| W6 | **Proactive timer**: every 30 s the agent expires unclaimed holds, asks the BookingAgent to release them, notifies the person, and re-offers the seats to the next person, all without user action. |
| W7 | On `event_cancelled` all entries are removed and holds released. |

Reactive: subscribes to `seats_released` and `event_cancelled` broadcasts.

## NotificationAgent: tell the right person what affects them

Subscribes to **every** broadcast. Turns `booking_confirmed`, `booking_cancelled`, `event_cancelled`, `waitlist_offer`, `waitlist_offer_expired`, `hire_confirmed`, `hire_escalated` (attendee and all admins), `hire_approved`, `hire_rejected` and `suspicious_scan` (admins) into notifications. Ignores topics that affect no specific person (e.g. `seats_released`) and counts them. Beliefs: notifications created per topic.

## HirerAgent: get the attendee an affordable venue (personal assistant agent)

| ID | Rule |
|----|------|
| H1 | Only accept an offer within the attendee's budget, time flexibility (±0–3 h) and venue choice. |
| H2 | Opening bid = 80 % of the asking price, +7 % per round, never above the budget. Accept the VenueAgent's counter as soon as it fits the budget. |
| H3 | Give up after 4 rounds and PROPOSE the best option (list price) instead of a bare refusal. |
| H4 | Rank feasible slots: closest to the requested time first, then the requested venue, then the cheapest. |

Protocol: `REQUEST quote_options` → `PROPOSE` (feasible slots at list price) → up to 4 × (`PROPOSE counter_offer` ↔ `PROPOSE`/`AGREE`) → `REQUEST hire_request` with `agreed_price` → `AGREE`/`INFORM` (escalated) → notification broadcast. Example: list price Rs. 3,000, budget Rs. 2,700, weekday morning. The HirerAgent bids 2,400, the VenueAgent counters 2,700 (halfway, above its 2,550 floor), and the HirerAgent accepts. The attendee saves Rs. 300.

## Concurrency approach

* Seat allocation uses a single atomic `UPDATE … WHERE sold + held + q <= quantity` and checks `rowcount`. In PostgreSQL the row lock makes the second buyer wait and then re-check the condition against the committed value, so only one can take the last seat. In SQLite, writers are serialised. `test_two_buyers_for_the_last_seat_at_the_same_moment` runs two threads at once to prove it.
* A database CHECK constraint `sold + held <= quantity` is the last line of defence.
* Belief updates are guarded by a per-agent lock. The app runs as one worker process.
