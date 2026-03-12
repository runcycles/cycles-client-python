"""Basic programmatic usage of the Cycles client."""

from runcycles import (
    Action,
    Amount,
    CommitRequest,
    CyclesClient,
    CyclesConfig,
    CyclesMetrics,
    ReservationCreateRequest,
    ReleaseRequest,
    Subject,
    Unit,
)


def main() -> None:
    config = CyclesConfig(
        base_url="http://localhost:7878",
        api_key="your-api-key",
        tenant="acme",
    )

    with CyclesClient(config) as client:
        # Full reserve → execute → commit lifecycle
        response = client.create_reservation(ReservationCreateRequest(
            idempotency_key="req-001",
            subject=Subject(tenant="acme", agent="support-bot"),
            action=Action(kind="llm.completion", name="gpt-4"),
            estimate=Amount(unit=Unit.USD_MICROCENTS, amount=500_000),
            ttl_ms=30_000,
        ))

        print(f"Reservation: success={response.is_success}, body={response.body}")

        if not response.is_success:
            print(f"Failed: {response.error_message}")
            return

        reservation_id = response.get_body_attribute("reservation_id")
        print(f"Reserved: {reservation_id}")

        # Simulate work
        result = "Generated response text"

        # Commit actual usage
        commit_response = client.commit_reservation(reservation_id, CommitRequest(
            idempotency_key="commit-001",
            actual=Amount(unit=Unit.USD_MICROCENTS, amount=420_000),
            metrics=CyclesMetrics(
                tokens_input=1200,
                tokens_output=800,
                latency_ms=150,
                model_version="gpt-4-0613",
            ),
        ))
        print(f"Commit: success={commit_response.is_success}, body={commit_response.body}")

        # Query balances
        balances = client.get_balances(tenant="acme")
        print(f"Balances: {balances.body}")


if __name__ == "__main__":
    main()
