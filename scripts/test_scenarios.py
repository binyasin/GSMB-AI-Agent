"""Quick scenario runner: see how the conversation engine handles different
customer responses, without touching the database or the Google Sheet.

Usage:
  python scripts/test_scenarios.py                # run every scenario
  python scripts/test_scenarios.py already_paid    # run just one
  python scripts/test_scenarios.py --list          # list scenario names

Uses the TEST_* fixture consumer (same one `run_test_call` uses) and the
same conversation engine the real app uses -- just without persisting
anything, so it's safe to run as many times as you like.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.conversation_engine import ConversationEngine  # noqa: E402
from app.schemas import ConsumerRecord, SupportedLanguage  # noqa: E402

SCENARIOS: dict[str, list[str]] = {
    "cooperative_promise": [
        "Ji han, main hi hoon.",
        "Ji, main is hafte tak pay karne ki koshish karoon ga.",
        "Agle Monday tak.",
    ],
    "pays_today": [
        "Ji han, main hi hoon.",
        "Main aaj hi pay kar dun ga.",
    ],
    "already_paid": [
        "Ji han, main hi hoon.",
        "Maine ye bill already pay kar diya hai pichle hafte.",
    ],
    "wrong_number": [
        "Nahi, ye ghalat number hai, aap kis se baat karna chahte hain?",
    ],
    "do_not_call": [
        "Ji han, main hi hoon.",
        "Mujhe dobara kabhi call mat karna.",
    ],
    "dispute": [
        "Ji han, main hi hoon.",
        "Ye bill ka amount galat hai, main is se agree nahi karta.",
    ],
    "installment_request": [
        "Ji han, main hi hoon.",
        "Mujhe installment plan chahiye, poori amount ek sath nahi de sakta.",
    ],
    "human_assistance": [
        "Ji han, main hi hoon.",
        "Mujhe kisi insaan/representative se baat karni hai.",
    ],
    "not_interested": [
        "Ji han, main hi hoon.",
        "Mujhe is mein koi interest nahi hai.",
    ],
}


def run_scenario(name: str, replies: list[str]) -> None:
    settings = get_settings()
    consumer = ConsumerRecord(
        consumer_no="TEST-CONSUMER",
        consumer_name=settings.test_consumer_name,
        mobile_number=settings.test_phone_number,
        outstanding_amount=float(settings.test_outstanding_amount),
        installment_eligible=True,
        installment_details=settings.test_scheme,
    )
    engine = ConversationEngine(consumer, language=SupportedLanguage.URDU)

    print(f"\n{'=' * 70}\nSCENARIO: {name}\n{'=' * 70}")
    line = engine.start()
    print(f"Agent   : {line}")
    for reply in replies:
        print(f"Customer: {reply}")
        line = engine.respond(reply)
        if line:
            print(f"Agent   : {line}")
        if engine.stage.value == "ENDED":
            break

    d = engine.decision
    print("-" * 70)
    print(f"  intent={d.intent.value}  human_followup={d.human_followup}  "
          f"do_not_call={d.do_not_call}  verification_passed={d.verification_passed}")
    if d.promise_to_pay_date:
        print(f"  promise_to_pay_date={d.promise_to_pay_date}")
    if d.notes:
        print(f"  notes={d.notes!r}")


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--list":
        for name in SCENARIOS:
            print(name)
        return

    targets = args if args else list(SCENARIOS.keys())
    unknown = [t for t in targets if t not in SCENARIOS]
    if unknown:
        print(f"Unknown scenario(s): {unknown}. Run with --list to see valid names.")
        sys.exit(1)

    for name in targets:
        run_scenario(name, SCENARIOS[name])


if __name__ == "__main__":
    main()
