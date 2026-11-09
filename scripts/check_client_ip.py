"""Check that the deployed app identifies clients correctly behind its proxies.

Sends over-length messages, each with a different forged X-Forwarded-For header. They
count towards the per-client rate limit but are rejected before any model call, so the
check costs nothing.

    uv run python scripts/check_client_ip.py https://your-app.onrender.com

Step 1 (one network): the request after the limit must be rate limited. If none is,
forged headers are being trusted: TRUSTED_PROXY_COUNT is too high.

Step 2 (a second network, e.g. a phone hotspot, within a minute of step 1):

    uv run python scripts/check_client_ip.py https://your-app.onrender.com --requests 1

It must NOT be rate limited. If it is, different visitors share one bucket (probably a
proxy's address): TRUSTED_PROXY_COUNT is too low.
"""
import argparse
import json
import random
import sys
import urllib.request

RATE_LIMITED = "Too many requests"
REJECTED_TOO_LONG = "too long"


def _forged_ip() -> str:
    # TEST-NET-3 (203.0.113.0/24): documentation range, never a real client
    return f"203.0.113.{random.randint(1, 254)}"


def call_chat(base_url: str, api_name: str, message: str, forwarded_for: str, timeout: float) -> str:
    """One call to a Gradio endpoint via its REST API; returns the reply text."""
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "digital-twin-ip-check",
        "X-Forwarded-For": forwarded_for,
    }
    start = urllib.request.Request(
        f"{base_url}/gradio_api/call/{api_name}",
        data=json.dumps({"data": [message]}).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(start, timeout=timeout) as response:
        event_id = json.load(response)["event_id"]

    result = urllib.request.Request(f"{base_url}/gradio_api/call/{api_name}/{event_id}", headers=headers)
    event = None
    with urllib.request.urlopen(result, timeout=timeout) as stream:
        for raw in stream:
            line = raw.decode("utf-8").strip()
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event in ("complete", "error"):
                data = json.loads(line.split(":", 1)[1])
                if event == "error":
                    raise RuntimeError(f"Gradio returned an error: {data}")
                return str(data[0]) if isinstance(data, list) and data else str(data)
    raise RuntimeError("stream ended without a result")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", help="base URL of the deployed app")
    parser.add_argument("--requests", type=int, default=31,
                        help="requests to send (default 31 = GUARD_RATE_LIMIT_REQUESTS default + 1)")
    parser.add_argument("--max-message-length", type=int, default=500,
                        help="the app's GUARD_MAX_MESSAGE_LENGTH (default 500)")
    parser.add_argument("--api-name", default="chat")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument(
        "--simulate-proxy-hops", default="",
        help="local testing only: entries to append after the forged one, imitating what the "
             "proxies add in production (e.g. '99.61.165.29, 104.22.17.40'). Locally there are no "
             "proxies, and uvicorn trusts X-Forwarded-For from 127.0.0.1, so without this the "
             "check fails even when the app is right.",
    )
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    message = "x" * (args.max_message_length + 1)
    first_limited = None
    for i in range(1, args.requests + 1):
        forwarded_for = ", ".join(h for h in [_forged_ip(), args.simulate_proxy_hops] if h)
        reply = call_chat(base_url, args.api_name, message, forwarded_for, args.timeout)
        if RATE_LIMITED in reply:
            first_limited = first_limited or i
            outcome = "rate limited"
        elif REJECTED_TOO_LONG in reply.lower():
            outcome = "rejected as too long (counted, no model call)"
        else:
            outcome = "UNEXPECTED reply; is --max-message-length right?"
        print(f"{i:>3}: {outcome}")

    print()
    if args.requests == 1:
        if first_limited:
            print("FAIL: rate limited on the first request. If step 1 just ran from another network, "
                  "clients share a bucket: TRUSTED_PROXY_COUNT is probably too low.")
            return 1
        print("PASS: not rate limited, so this network has its own bucket.")
        return 0
    if first_limited is None:
        print("FAIL: never rate limited despite exceeding the limit. Forged X-Forwarded-For values "
              "are being used as the client key: TRUSTED_PROXY_COUNT is probably too high.")
        return 1
    print(f"PASS: rate limited from request {first_limited}, despite a new forged header on every request.")
    print("Now run step 2 from a different network within a minute (see --help).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
