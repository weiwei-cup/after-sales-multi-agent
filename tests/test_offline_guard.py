import socket

import pytest
from pytest_socket import SocketBlockedError


@pytest.mark.unit
@pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6])
def test_asyncio_unix_socket_exception_keeps_internet_sockets_blocked(family):
    with pytest.warns(UserWarning, match="socket.socket"), pytest.raises(SocketBlockedError):
        socket.socket(family, socket.SOCK_STREAM)
