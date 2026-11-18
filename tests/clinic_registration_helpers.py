"""Test-only helpers for the clinic approval and one-time account activation flow."""

from urllib.parse import parse_qs, urlsplit


def approve_and_activate(client, request_id, username, password, admin_headers):
    """Approve without receiving a password, then use the returned one-time link."""
    approval = client.post(
        f"/api/admin/registrations/{request_id}/approve",
        headers=admin_headers,
        json={"login_username": username},
    )
    assert approval.status_code == 200, approval.text[:300]
    payload = approval.json()
    activation_path = payload.get("activation_path", "")
    parsed = urlsplit(activation_path)
    assert parsed.path == "/clinic/activate" and not parsed.query
    token_values = parse_qs(parsed.fragment).get("token", [])
    assert len(token_values) == 1 and len(token_values[0]) >= 40

    activated = client.post(
        "/api/clinic/account/activate",
        json={"token": token_values[0], "password": password},
    )
    assert activated.status_code == 200, activated.text[:300]
    return payload, activated.json()
