"""HirerAgent - the attendee's personal assistant agent (creative extension).
Goal: get the attendee a venue they can afford, as close as possible to the time they asked for.
It negotiates with the VenueAgent using alternating offers (PROPOSE / counter-PROPOSE / AGREE)."""
from sqlalchemy.orm import Session

from . import rules as R
from .base import Agent
from .messages import Message, Performative as P


class HirerAgent(Agent):
    name = "HirerAgent"
    goal = "Get the attendee an affordable venue, as close as possible to the requested time"
    default_beliefs = {"negotiations": 0, "deals": 0, "no_deals": 0, "rounds_total": 0, "saved_total": 0.0}

    def handle_negotiate_hire(self, msg: Message, db: Session) -> Message:
        c = msg.content
        conv = msg.conversation_id
        budget = float(c.get("budget") or 0)
        if budget <= 0:
            return self.decide(msg, P.REFUSE, "H1", reason="Give a budget greater than zero")
        self.bump("negotiations")

        # 1. Ask the VenueAgent for every feasible slot inside the attendee's flexibility.
        quote = self.request(db, "VenueAgent", "quote_options", conv, venue_id=c["venue_id"], start=c["start"],
                             end=c["end"], guests=c["guests"], flex_hours=c.get("flex_hours", 0),
                             any_venue=bool(c.get("any_venue")))
        if quote.performative != P.PROPOSE:
            self.bump("no_deals")
            return self.decide(msg, P.REFUSE, "H1", reason="No free slot fits your request: "
                                                            + quote.content.get("reason", ""))
        # 2. H4: rank offers - closest to the requested time, the requested venue, then cheapest.
        offers = sorted(quote.content["alternatives"],
                        key=lambda o: (abs(o["shift_minutes"]), o["venue_id"] != int(c["venue_id"]), o["price"]))
        rounds: list[dict] = []
        affordable = [o for o in offers if o["price"] <= budget]
        chosen = affordable[0] if affordable else offers[0]
        list_price = chosen["price"]
        agreed = None
        if affordable:
            agreed = list_price
            rounds.append({"round": 0, "who": "VenueAgent", "price": list_price, "note": "list price is within budget"})
        else:
            rounds.append({"round": 0, "who": "VenueAgent", "price": list_price, "note": "list price"})
            slot = {k: chosen[k] for k in ("venue_id", "venue_name", "start", "end")}
            for r in range(R.NEGOTIATION_MAX_ROUNDS):  # H2 / H3
                bid = round(min(budget, list_price * (R.HIRER_OPENING_RATIO + R.HIRER_STEP * r)), 2)
                rounds.append({"round": r + 1, "who": "HirerAgent", "price": bid})
                answer = self.bus.send(Message(sender=self.name, receiver="VenueAgent", performative=P.PROPOSE,
                                               conversation_id=conv,
                                               content={"action": "counter_offer", "round": r + 1,
                                                        "reason": f"Counter-offer {r + 1}: within the attendee's budget",
                                                        "alternatives": [dict(slot, price=bid)]}), db)
                if answer.performative == P.AGREE:
                    agreed = bid
                    rounds.append({"round": r + 1, "who": "VenueAgent", "price": bid, "note": "accepted"})
                    break
                if answer.performative != P.PROPOSE:
                    break
                ask = answer.content["alternatives"][0]["price"]
                rounds.append({"round": r + 1, "who": "VenueAgent", "price": ask, "note": "counter-offer"})
                if ask <= budget:  # H1: the venue's counter fits the budget -> accept it
                    agreed = ask
                    break
            self.bump("rounds_total", len([x for x in rounds if x["who"] == "HirerAgent"]))

        if agreed is None:  # H3: no deal - report the best option instead of a bare refusal
            self.bump("no_deals")
            last_ask = rounds[-1]["price"]
            return self.decide(msg, P.PROPOSE, "H3", rounds=rounds, budget=budget,
                               reason=f"No deal within your budget of Rs. {budget:,.2f} after "
                                      f"{R.NEGOTIATION_MAX_ROUNDS} rounds (venue's best: Rs. {last_ask:,.2f})",
                               alternatives=[dict(chosen, note="request at list price, without a discount")])

        # 3. Deal: ask the VenueAgent to book at the agreed price.
        booking = self.request(db, "VenueAgent", "hire_request", conv, user_id=c["user_id"],
                               venue_id=chosen["venue_id"], start=chosen["start"], end=chosen["end"],
                               guests=c["guests"], purpose=c.get("purpose", ""),
                               agreed_price=None if agreed == list_price else agreed)
        content = {k: v for k, v in booking.content.items() if k not in ("action", "rule", "rule_text", "evidence")}
        saved = round(list_price - agreed, 2)
        if booking.performative in (P.AGREE, P.INFORM):
            self.bump("deals")
            self.bump("saved_total", saved)
        return self.decide(msg, booking.performative, "H1", rounds=rounds, budget=budget, list_price=list_price,
                           saved=saved, venue_rule=booking.content.get("rule"), **content)
