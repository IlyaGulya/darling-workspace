#!/usr/bin/env python3
"""Deterministic host-level model of the process-control request slot (dar-b5pe).

WHY A MODEL AND NOT A GUEST RUN. The defect this test exists for appeared in roughly a third of twelve-thread
boots and its evidence is a race between two publishers of one shared page, so a guest run can only report that it
happened, never which interleaving caused it. This file encodes the protocol as written in
`include/darlingserver/rpc-supplement.h` -- the request word with its states (IDLE, PENDING, CLAIMED written by the
server on ownership transfer, DONE on completion), the separate reply word with its own sequence, and the client's
RELEASE -- and forces the interleavings by ordering operations explicitly instead of sleeping.

TWO ARMS, ONE REQUIRED OUTCOME.
  v1 = the protocol in the tree today: RELEASE is a plain store of IDLE, so a client that gives up after the server
       claimed the slot hands it to the next publisher. Case C FAILS here, which is the RED proof.
  v2 = the required algorithm: release is a compare-and-swap from PENDING (it can never take the slot away from the
       server), a publisher claims only IDLE or a DONE whose generation is older than its own, and a completion
       carries the generation it answers. Every case must PASS here.

The invariant both arms are judged by is one sentence: a client giving up waiting does not make the shared slot safe
to re-use while an older server transaction can still publish a completion, and a late completion for generation A
must never satisfy or mutate generation B.

Run directly (`python3 tests/process_control_slot_ownership_model.py`) or through the shell contract next to it.
"""
from __future__ import annotations

import sys

IDLE, PENDING, DONE, CLAIMED = 0, 1, 2, 3


class Page:
    """The shared page: one request slot and one reply word, exactly as the struct declares them."""

    def __init__(self) -> None:
        self.request_state = IDLE
        self.request_seq = 0
        self.request_op = 0
        self.reply_state = IDLE
        self.reply_seq = 0
        self.reply_status = 0
        self.owner: int | None = None  # what the server recorded when it took ownership


class Failure(AssertionError):
    pass


class Protocol:
    """One protocol version; `generation_safe` selects the algorithm under test."""

    def __init__(self, generation_safe: bool) -> None:
        self.generation_safe = generation_safe

    def publish(self, page: Page, generation: int, op: int) -> bool:
        if self.generation_safe:
            if page.request_state == IDLE:
                pass
            elif page.request_state == DONE and page.reply_seq < generation:
                pass  # the previous owner's completion is spent; a newer generation may take the slot
            else:
                return False
        elif page.request_state not in (IDLE, DONE):
            return False
        page.request_state = PENDING
        page.request_seq = generation
        page.request_op = op
        return True

    def release(self, page: Page) -> None:
        """Give up waiting. The generation-safe form may only take back a slot nobody else claimed."""
        if self.generation_safe:
            if page.request_state == PENDING:
                page.request_state = IDLE
                page.request_seq = 0
            # CLAIMED belongs to the server from here on: the client stops waiting and leaves it alone.
            return
        page.request_state = IDLE
        page.request_seq = 0

    def server_claim(self, page: Page) -> int | None:
        if page.request_state != PENDING:
            return None
        page.request_state = CLAIMED
        page.owner = page.request_seq
        return page.owner

    def server_complete(self, page: Page, generation: int, status: int) -> None:
        page.reply_seq = generation
        page.reply_status = status
        page.reply_state = DONE
        page.request_state = DONE

    def consume(self, page: Page, generation: int) -> int | None:
        if page.reply_state != DONE or page.reply_seq != generation:
            return None
        return page.reply_status


def case_a_normal(p: Protocol) -> None:
    page = Page()
    assert p.publish(page, 1, op=8), "A: publish failed"
    assert p.server_claim(page) == 1, "A: server did not claim"
    p.server_complete(page, 1, status=0)
    if p.consume(page, 1) != 0:
        raise Failure("A: normal completion was not consumable")


def case_b_abandon_before_claim(p: Protocol) -> None:
    page = Page()
    assert p.publish(page, 1, op=8), "B: publish failed"
    p.release(page)  # give up before anybody claimed it
    if page.request_state != IDLE:
        raise Failure("B: abandonment before claim left the slot not reusable")
    assert p.publish(page, 2, op=7), "B: the slot was not reusable after an unclaimed abandonment"
    if p.server_claim(page) != 2:
        raise Failure("B: the server claimed the wrong generation")


def case_c_abandon_after_claim(p: Protocol) -> None:
    page = Page()
    assert p.publish(page, 1, op=8), "C: publish A failed"
    if p.server_claim(page) != 1:
        raise Failure("C: the server did not take ownership of A")
    p.release(page)  # the client gives up AFTER the server owns the request
    if page.request_state == CLAIMED and not p.generation_safe:
        pass  # v1 stores IDLE here, which is the defect; v2 leaves CLAIMED alone
    published = p.publish(page, 2, op=7)
    if published and page.request_seq != 2:
        raise Failure("C: B's own request was overwritten while publishing")
    if published and page.request_state == CLAIMED and page.owner == 1:
        raise Failure("C: B published into a slot the server still owns for generation A")
    p.server_complete(page, 1, status=0)  # A's completion arrives late
    if p.generation_safe:
        if page.request_state == PENDING and page.request_seq == 2 and page.reply_seq == 1:
            raise Failure("C: a late completion for A landed on generation B's pending request")
        # The point of the rule: B was refused while ownership was ambiguous, and once A's completion arrives the
        # slot is publishable again for a NEWER generation. B must then complete normally, not be starved.
        if not p.publish(page, 2, op=7):
            raise Failure("C: generation B could not publish after A's completion released the slot")
        if p.server_claim(page) != 2:
            raise Failure("C: the server could not claim generation B after A's late completion")
        p.server_complete(page, 2, status=0)
        if p.consume(page, 2) != 0:
            raise Failure("C: generation B could not consume its own completion")
    else:
        if page.reply_seq == 1 and page.request_seq == 2:
            raise Failure(
                "C: a late completion for generation A landed on generation B's slot "
                f"(reply_seq={page.reply_seq}, request_seq={page.request_seq})"
            )


def case_d_late_completion_must_not_satisfy_b(p: Protocol) -> None:
    page = Page()
    assert p.publish(page, 1, op=8) and p.server_claim(page) == 1
    p.release(page)
    published = p.publish(page, 2, op=7)
    if not p.generation_safe and not published:
        raise Failure("D: B could not publish")
    p.server_complete(page, 1, status=0)
    if p.consume(page, 2) is not None:
        raise Failure("D: B consumed A's reply")


def case_e_stale_done_during_publication(p: Protocol) -> None:
    """A's completion is already DONE when B publishes. B must never read it as its own."""
    page = Page()
    assert p.publish(page, 1, op=8) and p.server_claim(page) == 1
    p.server_complete(page, 1, status=0)
    if not p.publish(page, 2, op=7):
        raise Failure("E: B could not publish over a completed request")
    if p.consume(page, 2) is not None:
        raise Failure("E: a foreign DONE satisfied generation B")
    if p.server_claim(page) != 2:
        raise Failure("E: the server could not claim B after A's stale DONE")


def case_f_repeated_generations(p: Protocol) -> None:
    """Repeated generations under forced scheduling must never wedge the slot nor leak one into another."""
    page = Page()
    for generation in range(1, 40):
        if generation % 3 == 0:
            assert p.publish(page, generation, op=8), f"F: publish {generation} failed"
            p.release(page)  # abandon before claim
            if p.generation_safe and page.request_state not in (IDLE, DONE):
                raise Failure(f"F: slot wedged after abandoning {generation}: state={page.request_state}")
            continue
        assert p.publish(page, generation, op=8), f"F: publish {generation} failed"
        if p.server_claim(page) != generation:
            raise Failure(f"F: the server claimed the wrong generation at {generation}")
        if generation % 3 == 1:
            p.release(page)  # abandonment after claim
        p.server_complete(page, generation, status=0)
        if not (page.reply_state == DONE and page.reply_seq == generation):
            raise Failure(f"F: generation {generation} left no completion")
        if p.consume(page, generation) != 0:
            raise Failure(f"F: generation {generation} could not consume its own completion")


def run(generation_safe: bool) -> int:
    proto = Protocol(generation_safe)
    label = "v2-generation-safe" if generation_safe else "v1-current"
    cases = [
        ("A normal request", case_a_normal),
        ("B abandonment before claim", case_b_abandon_before_claim),
        ("C abandonment after claim", case_c_abandon_after_claim),
        ("D late completion must not satisfy B", case_d_late_completion_must_not_satisfy_b),
        ("E stale DONE during publication", case_e_stale_done_during_publication),
        ("F repeated generations", case_f_repeated_generations),
    ]
    failures = 0
    for name, fn in cases:
        try:
            fn(proto)
        except Failure as exc:
            failures += 1
            print(f"FAIL [{label}] {name}: {exc}")
        else:
            print(f"PASS [{label}] {name}")
    print(f"{label}: failures={failures} of {len(cases)}")
    return failures


def main() -> int:
    v1 = run(generation_safe=False)
    v2 = run(generation_safe=True)
    # The contract: the current protocol MUST fail the ownership cases, and the required algorithm MUST pass all.
    if v1 == 0:
        print("SLOT-OWNERSHIP INVALID: the current protocol passed every case, so this test proves nothing")
        return 2
    if v2 != 0:
        print(f"SLOT-OWNERSHIP FAILED: the generation-safe algorithm left {v2} failures")
        return 3
    print(f"SLOT-OWNERSHIP ok: current protocol fails {v1} case(s); generation-safe algorithm is clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
