from unittest.mock import MagicMock

import pytest

from scripts.provision_shared_account import provision


def connection_with(*results):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = results
    return connection, cursor


def test_provision_refuses_existing_role_without_mutations():
    connection, cursor = connection_with(("orders", "owner"), (True,))
    with pytest.raises(ValueError, match="already exists"):
        provision(connection, "x" * 32)
    assert cursor.execute.call_count == 2
    assert connection.transaction.return_value.__exit__.call_args.args[0] is ValueError


def test_provision_rolls_back_if_original_tables_are_exposed():
    connection, cursor = connection_with(("orders", "owner"), (False,), (False,), (True,))
    with pytest.raises(ValueError, match="PUBLIC grants"):
        provision(connection, "x" * 32)
    assert connection.transaction.return_value.__exit__.call_args.args[0] is ValueError


def test_provision_success_does_not_return_password():
    connection, cursor = connection_with(("orders", "owner"), (False,), (False,), (False,))
    password = "x" * 32
    assert password not in provision(connection, password)
    statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
    assert "CREATE SCHEMA kayhap" in statements
    assert "GRANT USAGE, CREATE ON SCHEMA kayhap TO p2p_kayhap" in statements
    assert not any("SET ROLE" in statement or "AUTHORIZATION" in statement for statement in statements)
    assert cursor.execute.call_count == 9
    assert connection.transaction.return_value.__exit__.call_args.args[0] is None
