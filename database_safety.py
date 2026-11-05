"""
database_safety.py
==================
Centralized, fail-closed database safety validation for PrimeCare Clinic.
Guarantees production Neon database is NEVER targeted by migrations, scripts, or test suites.

Strict Invariants Enforced:
  1. Identifies database hosts via urllib.parse.
  2. Normalizes hostname only (lowercase); never modifies case-sensitive passwords or credentials.
  3. Always rejects known production endpoint 'ep-small-dream'.
  4. Always rejects any connection matching production DATABASE_URL identity.
  5. Allows Neon connections ONLY when:
       - PRIMECARE_ALLOW_DISPOSABLE_NEON_TEST_DB == '1'
       - Target matches POSTGRES_TEST_URL
       - Target host, port, database, and username strictly match POSTGRES_TEST_URL.
  6. Rejects arbitrary Neon endpoints even when the authorization flag is enabled.
  7. Never silently falls back to DATABASE_URL.
  8. Never allows --allow-dev-db or any other flag to bypass Neon production prohibitions.
  9. Never prints database credentials, passwords, or complete connection strings.
  10. Protects local SQLite production database (instance/primecare.db) requiring --allow-dev-db.
"""

import os
from urllib.parse import urlparse
from typing import Optional

KNOWN_PRODUCTION_ENDPOINT = "ep-small-dream"


def assert_database_safety(db_uri: str, allow_dev_db: bool = False) -> None:
    """
    Enforces strict database safety.
    Raises RuntimeError or ValueError if the target database is dangerous, unauthorized, or production.
    """
    if not db_uri or not isinstance(db_uri, str) or not db_uri.strip():
        raise ValueError("Database URI cannot be empty or invalid.")

    # 1. Parse URL (preserve case of credentials and query parameters)
    raw_uri = db_uri.strip()
    parsed = urlparse(raw_uri)
    scheme = (parsed.scheme or "").lower()
    hostname = (parsed.hostname or "").lower()
    port = parsed.port
    db_name = parsed.path.lstrip('/')
    username = parsed.username

    # 2. Always reject known production endpoint anywhere in URI or hostname
    if KNOWN_PRODUCTION_ENDPOINT in hostname or KNOWN_PRODUCTION_ENDPOINT in raw_uri.lower():
        raise RuntimeError(
            f"CRITICAL SAFETY ABORT: Target database matches known production endpoint '{KNOWN_PRODUCTION_ENDPOINT}'! "
            "Connecting to production Neon database is strictly prohibited."
        )

    # 3. Reject any connection matching production DATABASE_URL host
    prod_env_url = os.environ.get("DATABASE_URL", "").strip()
    if prod_env_url:
        prod_parsed = urlparse(prod_env_url)
        prod_host = (prod_parsed.hostname or "").lower()
        if prod_host and prod_host not in ('127.0.0.1', 'localhost'):
            if hostname == prod_host:
                raise RuntimeError(
                    "CRITICAL SAFETY ABORT: Target database host matches production DATABASE_URL host! "
                    "Execution rejected to prevent production tampering."
                )

    # 4. Handle Neon connections (*.neon.tech or host containing 'neon')
    if "neon.tech" in hostname or (hostname and "neon" in hostname):
        # Must have explicit authorization flag enabled
        allow_flag = os.environ.get("PRIMECARE_ALLOW_DISPOSABLE_NEON_TEST_DB", "").strip()
        if allow_flag != "1":
            raise RuntimeError(
                "CRITICAL SAFETY ABORT: Neon database connection rejected. "
                "PRIMECARE_ALLOW_DISPOSABLE_NEON_TEST_DB must be set to '1' for authorized disposable test runs."
            )

        # Must have POSTGRES_TEST_URL configured
        configured_test_url = os.environ.get("POSTGRES_TEST_URL", "").strip()
        if not configured_test_url:
            raise RuntimeError(
                "CRITICAL SAFETY ABORT: Neon connection rejected. "
                "POSTGRES_TEST_URL is not configured in environment."
            )

        # Parse configured test URL
        parsed_conf = urlparse(configured_test_url)
        conf_host = (parsed_conf.hostname or "").lower()
        conf_port = parsed_conf.port or 5432
        conf_db = parsed_conf.path.lstrip('/')
        conf_user = parsed_conf.username

        eff_port = port or 5432

        # Verify host
        if not conf_host or hostname != conf_host:
            raise RuntimeError(
                "CRITICAL SAFETY ABORT: Target Neon hostname does not match configured POSTGRES_TEST_URL host. "
                "Arbitrary Neon endpoints are strictly prohibited."
            )

        # Verify port
        if eff_port != conf_port:
            raise RuntimeError(
                f"CRITICAL SAFETY ABORT: Target port ({eff_port}) does not match configured POSTGRES_TEST_URL port ({conf_port})."
            )

        # Verify database name
        if db_name != conf_db:
            raise RuntimeError(
                f"CRITICAL SAFETY ABORT: Target database name does not match configured POSTGRES_TEST_URL database name."
            )

        # Verify username
        if conf_user and username != conf_user:
            raise RuntimeError(
                "CRITICAL SAFETY ABORT: Target username does not match configured POSTGRES_TEST_URL username."
            )

        # Explicitly authorized disposable test Neon database confirmed
        return

    # 5. Local PostgreSQL handling (127.0.0.1 / localhost)
    if 'postgres' in scheme:
        if hostname not in ('127.0.0.1', 'localhost'):
            raise RuntimeError(
                f"CRITICAL SAFETY ABORT: Non-local, non-disposable PostgreSQL host rejected: '{hostname}'."
            )
        return

    # 6. SQLite handling
    if scheme == 'sqlite':
        clean_path = db_name.lower().replace('\\', '/')
        if 'primecare.db' in clean_path and 'test_primecare.db' not in clean_path:
            if not allow_dev_db:
                raise RuntimeError(
                    "CRITICAL SAFETY ABORT: Modifying instance/primecare.db requires explicit --allow-dev-db flag "
                    "and an existing verified backup."
                )
        return

    # Reject any other unrecognized scheme/host
    raise RuntimeError(f"CRITICAL SAFETY ABORT: Unrecognized or unsupported database target scheme: '{scheme}'.")

