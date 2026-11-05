"""
tests/test_database_safety.py
=============================
Automated Safety Unit Tests for Database Safety Guards (Phase 3B.2.3).
Verifies that production Neon database is completely protected and only
explicitly authorized disposable test environments can proceed.
"""

import os
import unittest
from unittest.mock import patch

from database_safety import assert_database_safety, KNOWN_PRODUCTION_ENDPOINT


class DatabaseSafetyTestSuite(unittest.TestCase):
    """Exhaustive safety suite validating database target protections."""

    def setUp(self):
        self.disposable_neon_url = "postgresql://neondb_owner:SecretPass123@ep-solitary-base-bfq0ef5z.c-5.us-west-2.aws.neon.tech/neondb?sslmode=require"
        self.prod_neon_url = "postgresql://primecare_admin:ProdPass999@ep-small-dream-12345678.us-east-2.aws.neon.tech/primecare_prod?sslmode=require"
        self.arbitrary_neon_url = "postgresql://attacker:FakePass@ep-arbitrary-hacker-999.us-east-1.aws.neon.tech/neondb?sslmode=require"
        self.local_sqlite_dev = "sqlite:///instance/primecare.db"
        self.local_sqlite_test = "sqlite:///instance/test_primecare.db"
        self.local_pg_test = "postgresql://postgres:postgres@127.0.0.1:5433/primecare_test"

    # 1. Correct disposable Neon URL accepted when authorized
    def test_01_correct_disposable_neon_url_accepted(self):
        env = {
            "PRIMECARE_ALLOW_DISPOSABLE_NEON_TEST_DB": "1",
            "POSTGRES_TEST_URL": self.disposable_neon_url,
            "DATABASE_URL": "sqlite:///instance/primecare.db"
        }
        with patch.dict(os.environ, env, clear=True):
            # Must succeed without exception
            assert_database_safety(self.disposable_neon_url)

    # 2. Production Neon URL rejected unconditionally
    def test_02_production_neon_url_rejected_unconditionally(self):
        env = {
            "PRIMECARE_ALLOW_DISPOSABLE_NEON_TEST_DB": "1",
            "POSTGRES_TEST_URL": self.disposable_neon_url,
            "DATABASE_URL": self.prod_neon_url
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                assert_database_safety(self.prod_neon_url)
            self.assertIn("CRITICAL SAFETY ABORT", str(ctx.exception))
            self.assertIn(KNOWN_PRODUCTION_ENDPOINT, str(ctx.exception))

    # 3. Production Neon URL rejected even if passed with --allow-dev-db
    def test_03_production_neon_url_rejected_even_with_allow_dev_db(self):
        env = {
            "PRIMECARE_ALLOW_DISPOSABLE_NEON_TEST_DB": "1",
            "POSTGRES_TEST_URL": self.prod_neon_url,
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                assert_database_safety(self.prod_neon_url, allow_dev_db=True)
            self.assertIn("CRITICAL SAFETY ABORT", str(ctx.exception))

    # 4. Authorization flag missing rejects disposable Neon
    def test_04_authorization_flag_missing_rejects_disposable_neon(self):
        env = {
            "PRIMECARE_ALLOW_DISPOSABLE_NEON_TEST_DB": "0",  # Disabled
            "POSTGRES_TEST_URL": self.disposable_neon_url
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                assert_database_safety(self.disposable_neon_url)
            self.assertIn("PRIMECARE_ALLOW_DISPOSABLE_NEON_TEST_DB must be set to '1'", str(ctx.exception))

    # 5. POSTGRES_TEST_URL missing rejects disposable Neon
    def test_05_postgres_test_url_missing_rejects_neon(self):
        env = {
            "PRIMECARE_ALLOW_DISPOSABLE_NEON_TEST_DB": "1",
            "POSTGRES_TEST_URL": ""
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                assert_database_safety(self.disposable_neon_url)
            self.assertIn("POSTGRES_TEST_URL is not configured", str(ctx.exception))

    # 6. Arbitrary Neon endpoint rejected even when flag is 1
    def test_06_arbitrary_neon_endpoint_rejected_when_flag_enabled(self):
        env = {
            "PRIMECARE_ALLOW_DISPOSABLE_NEON_TEST_DB": "1",
            "POSTGRES_TEST_URL": self.disposable_neon_url
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                assert_database_safety(self.arbitrary_neon_url)
            self.assertIn("does not match configured POSTGRES_TEST_URL host", str(ctx.exception))

    # 7. Mismatched database name rejected
    def test_07_mismatched_database_name_rejected(self):
        env = {
            "PRIMECARE_ALLOW_DISPOSABLE_NEON_TEST_DB": "1",
            "POSTGRES_TEST_URL": self.disposable_neon_url
        }
        tampered_db_url = self.disposable_neon_url.replace("/neondb", "/tampered_db")
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                assert_database_safety(tampered_db_url)
            self.assertIn("database name does not match configured POSTGRES_TEST_URL", str(ctx.exception))

    # 8. Mismatched username rejected
    def test_08_mismatched_username_rejected(self):
        env = {
            "PRIMECARE_ALLOW_DISPOSABLE_NEON_TEST_DB": "1",
            "POSTGRES_TEST_URL": self.disposable_neon_url
        }
        tampered_user_url = self.disposable_neon_url.replace("neondb_owner:", "other_user:")
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                assert_database_safety(tampered_user_url)
            self.assertIn("username does not match configured POSTGRES_TEST_URL", str(ctx.exception))

    # 9. Local SQLite test DB accepted unconditionally
    def test_09_local_sqlite_test_db_accepted(self):
        assert_database_safety(self.local_sqlite_test)

    # 10. Local SQLite production DB requires allow_dev_db flag
    def test_10_local_sqlite_dev_requires_allow_dev_db(self):
        with self.assertRaises(RuntimeError) as ctx:
            assert_database_safety(self.local_sqlite_dev, allow_dev_db=False)
        self.assertIn("Modifying instance/primecare.db requires explicit --allow-dev-db flag", str(ctx.exception))

        # With allow_dev_db=True, it succeeds
        assert_database_safety(self.local_sqlite_dev, allow_dev_db=True)

    # 11. Local PostgreSQL test DB (127.0.0.1:5433) accepted
    def test_11_local_postgres_test_db_accepted(self):
        assert_database_safety(self.local_pg_test)

    # 12. Remote non-Neon, non-local PostgreSQL rejected
    def test_12_remote_non_neon_postgres_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            assert_database_safety("postgresql://user:pass@192.168.1.100:5432/my_db")
        self.assertIn("Non-local, non-disposable PostgreSQL host rejected", str(ctx.exception))

    # 13. Empty or non-string URI rejected
    def test_13_empty_uri_rejected(self):
        with self.assertRaises(ValueError):
            assert_database_safety("")
        with self.assertRaises(ValueError):
            assert_database_safety(None)


if __name__ == "__main__":
    unittest.main()

