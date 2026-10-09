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
    assert cursor.execute.call_count == 8
    assert connection.transaction.return_value.__exit__.call_args.args[0] is None
